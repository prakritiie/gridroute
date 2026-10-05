"""Vectorised driver world (M1): N drivers drive straight lines to random NYC destinations.

All state is NumPy arrays indexed by driver number; one tick() moves every driver at once.
M2 replaces straight lines with OSRM routes x June congestion (spec §6.3); the interface stays the same.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from core.config import LAT_MAX, LAT_MIN, LNG_MAX, LNG_MIN

M_PER_DEG_LAT = 111_320.0

# Where trips start and end: (lat, lng, weight, spread_m). Manhattan-heavy like the 2016 taxi data.
HOTSPOTS = np.array([
    (40.7549, -73.9840, 0.20, 1200),   # Midtown
    (40.7127, -74.0059, 0.12, 900),    # Lower Manhattan
    (40.7736, -73.9566, 0.10, 900),    # Upper East Side
    (40.7870, -73.9754, 0.08, 900),    # Upper West Side
    (40.7359, -73.9911, 0.10, 800),    # Union Sq / Chelsea
    (40.8116, -73.9465, 0.04, 1000),   # Harlem
    (40.6928, -73.9903, 0.06, 1000),   # Downtown Brooklyn
    (40.7081, -73.9571, 0.06, 1000),   # Williamsburg
    (40.6720, -73.9778, 0.04, 1200),   # Park Slope
    (40.7447, -73.9485, 0.05, 900),    # Long Island City
    (40.7644, -73.9235, 0.03, 900),    # Astoria
    (40.6413, -73.7781, 0.06, 600),    # JFK
    (40.7769, -73.8740, 0.06, 500),    # LGA
])
_WEIGHTS = HOTSPOTS[:, 2] / HOTSPOTS[:, 2].sum()


@dataclass
class PingBatch:
    """Pings due this tick, as parallel arrays (what a batched WS frame will carry)."""
    idx: np.ndarray
    lat: np.ndarray
    lng: np.ndarray
    seq: np.ndarray

    def __len__(self) -> int:
        return len(self.idx)


def sample_points(rng: np.random.Generator, k: int) -> tuple[np.ndarray, np.ndarray]:
    """k points around the hotspots, weighted like 2016 taxi demand."""
    h = HOTSPOTS[rng.choice(len(HOTSPOTS), size=k, p=_WEIGHTS)]
    spread = h[:, 3]
    lat = h[:, 0] + rng.normal(0, 1, k) * spread / M_PER_DEG_LAT
    lng = h[:, 1] + rng.normal(0, 1, k) * spread / (M_PER_DEG_LAT * np.cos(np.radians(h[:, 0])))
    return np.clip(lat, LAT_MIN, LAT_MAX), np.clip(lng, LNG_MIN, LNG_MAX)


ROAM, TO_PICKUP, ON_TRIP = 0, 1, 2


class World:
    def __init__(
        self,
        n_drivers: int,
        seed: int = 0,
        ping_interval_s: int = 4,
        speed_mps: tuple[float, float] = (4.0, 10.0),
        gps_noise_m: float = 5.0,
        id_prefix: str = "d",
        seq_base: int = 0,
    ):
        self.n = n_drivers
        self.rng = np.random.default_rng(seed)
        self.ping_interval_s = ping_interval_s
        self.gps_noise_m = gps_noise_m
        self.ids = np.array([f"{id_prefix}{i}" for i in range(n_drivers)])

        self.lat, self.lng = self.sample_points(n_drivers)
        self.dest_lat, self.dest_lng = self.sample_points(n_drivers)
        self.speed = self.rng.uniform(*speed_mps, n_drivers)
        # move_driver.lua drops any seq <= the stored one, so a restarted app must not count from 1 again.
        # The sim passes the session start in ms: it grows ~4000x faster than one ping per 4 s.
        self.seq = np.full(n_drivers, seq_base, dtype=np.int64)
        self.alive = np.ones(n_drivers, dtype=bool)          # False = app died: stops pinging (chaos, C4)
        self.phase = self.rng.integers(0, ping_interval_s, n_drivers)   # stagger pings across ticks
        self.tick_no = 0

        # Trips: roaming -> to pickup -> on trip -> roaming (trip_done)
        self.index = {d: i for i, d in enumerate(self.ids)}
        self.mode = np.full(n_drivers, ROAM, dtype=np.int8)
        self.ride = np.full(n_drivers, None, dtype=object)
        self.drop_lat = np.zeros(n_drivers)
        self.drop_lng = np.zeros(n_drivers)
        self.conflicts = 0          # assignments that arrived for a driver already busy with another ride

    def sample_points(self, k: int) -> tuple[np.ndarray, np.ndarray]:
        return sample_points(self.rng, k)

    def assign(self, driver_id: str, ride_id: str, pickup, dropoff=None) -> bool:
        """True if the driver takes this ride now (then the caller sends `accept`). A repeat of an offer it
        already holds returns False, so it is never accepted twice."""
        i = self.index.get(driver_id)
        if i is None or not self.alive[i]:
            return False
        if self.mode[i] != ROAM:
            if self.ride[i] != ride_id:
                self.conflicts += 1     # the system offered a busy driver: would be a double-booking
            return False
        if dropoff is None:
            dlat, dlng = self.sample_points(1)
            dropoff = (dlat[0], dlng[0])
        self.mode[i], self.ride[i] = TO_PICKUP, ride_id
        self.dest_lat[i], self.dest_lng[i] = pickup
        self.drop_lat[i], self.drop_lng[i] = dropoff
        return True

    def cancel(self, driver_id: str, ride_id: str) -> bool:
        """The offer lapsed before the accept landed: drop the ride and roam again."""
        i = self.index.get(driver_id)
        if i is None or self.ride[i] != ride_id:
            return False
        self.mode[i], self.ride[i] = ROAM, None
        self.dest_lat[i], self.dest_lng[i] = (v[0] for v in self.sample_points(1))
        return True

    def tick(self, dt_s: float = 1.0) -> list[tuple[str, str]]:
        """Advance every live driver dt_s seconds toward its destination.

        Arrivals: roaming -> new random destination; at pickup -> head to dropoff; at dropoff -> trip done.
        Returns the (driver_id, ride_id) trips completed this tick.
        """
        cos_lat = np.cos(np.radians(self.lat))
        dy = (self.dest_lat - self.lat) * M_PER_DEG_LAT
        dx = (self.dest_lng - self.lng) * M_PER_DEG_LAT * cos_lat
        dist = np.hypot(dx, dy)
        step = self.speed * dt_s

        arrived = dist <= step
        frac = np.where(arrived, 1.0, step / np.maximum(dist, 1e-9))
        frac = np.where(self.alive, frac, 0.0)                  # dead apps stand still
        arrived &= self.alive
        self.lat = self.lat + (self.dest_lat - self.lat) * frac
        self.lng = self.lng + (self.dest_lng - self.lng) * frac

        at_pickup = arrived & (self.mode == TO_PICKUP)
        self.dest_lat[at_pickup], self.dest_lng[at_pickup] = self.drop_lat[at_pickup], self.drop_lng[at_pickup]
        self.mode[at_pickup] = ON_TRIP

        at_dropoff = arrived & (self.mode == ON_TRIP) & ~at_pickup
        done = [(str(self.ids[i]), self.ride[i]) for i in np.flatnonzero(at_dropoff)]
        self.mode[at_dropoff], self.ride[at_dropoff] = ROAM, None

        new_dest = (arrived & (self.mode == ROAM))
        if n := int(new_dest.sum()):
            self.dest_lat[new_dest], self.dest_lng[new_dest] = self.sample_points(n)
        self.tick_no += 1
        return done

    def due_pings(self) -> PingBatch:
        """Pings for live drivers whose 4 s slot is this tick. Increments their seq."""
        due = np.flatnonzero(self.alive & ((self.tick_no + self.phase) % self.ping_interval_s == 0))
        self.seq[due] += 1
        noise = self.rng.normal(0, self.gps_noise_m, (2, len(due)))
        lat = self.lat[due] + noise[0] / M_PER_DEG_LAT
        lng = self.lng[due] + noise[1] / (M_PER_DEG_LAT * np.cos(np.radians(self.lat[due])))
        return PingBatch(due, np.round(lat, 6), np.round(lng, 6), self.seq[due].copy())

    def kill(self, frac: float) -> np.ndarray:
        """Silence a random fraction of live drivers. Returns their indices."""
        live = np.flatnonzero(self.alive)
        dead = self.rng.choice(live, size=int(len(live) * frac), replace=False)
        self.alive[dead] = False
        return dead
