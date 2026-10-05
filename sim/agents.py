"""Replay drivers that follow real streets (M2).

A driver is idle where it last dropped off (no random cruising: empty km then means exactly the km driven
to pickups, and repositioning in R4 is the only other source). On an assignment it routes to the pickup
over OSRM, timed by June congestion; then carries the rider over the OSRM path in the trip's real recorded
duration; then frees itself with trip_done.

Positions are interpolated along the path by elapsed sim time, so every ping lands on a road.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field

from core.osrm import Route

IDLE, ROUTING, TO_PICKUP, ON_TRIP = "idle", "routing", "to_pickup", "on_trip"


def straight_route(frm: tuple[float, float], to: tuple[float, float], speed_mps: float = 8.0) -> Route:
    """Fallback when OSRM has no path: a straight segment with a road-like detour factor."""
    p1, p2 = math.radians(frm[0]), math.radians(to[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(to[1] - frm[1]) / 2) ** 2
    d = 2 * 6_371_000 * math.asin(math.sqrt(h))
    return Route(duration_s=1.3 * d / speed_mps, distance_m=1.3 * d, coords=[frm, to], cum=[0.0, d])


@dataclass
class Leg:
    kind: str                      # "pickup" | "trip"
    ride_id: str
    route: Route
    start_sim: float               # sim seconds
    duration_s: float              # true sim seconds for this leg

    def position(self, sim_t: float) -> tuple[float, float]:
        frac = min(1.0, max(0.0, (sim_t - self.start_sim) / self.duration_s)) if self.duration_s > 0 else 1.0
        target = frac * self.route.cum[-1]
        i = min(bisect.bisect_right(self.route.cum, target), len(self.route.coords) - 1)
        if i == 0:
            return self.route.coords[0]
        a, b = self.route.coords[i - 1], self.route.coords[i]
        seg = self.route.cum[i] - self.route.cum[i - 1]
        u = 0.0 if seg <= 0 else (target - self.route.cum[i - 1]) / seg
        return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)

    def done(self, sim_t: float) -> bool:
        return sim_t >= self.start_sim + self.duration_s


@dataclass
class Driver:
    id: str
    lat: float
    lng: float
    seq: int
    mode: str = IDLE
    ride_id: str | None = None
    leg: Leg | None = None
    empty_m: float = 0.0           # driven without a passenger (pickup legs)
    loaded_m: float = 0.0
    trips: int = 0


@dataclass
class Fleet:
    drivers: dict[str, Driver]
    conflicts: int = 0             # offers for a driver already busy with another ride (must stay 0)
    phase: dict[str, int] = field(default_factory=dict)

    @classmethod
    def at(cls, positions: list[tuple[float, float]], seq_base: int, prefix: str = "rd") -> "Fleet":
        ds = {f"{prefix}{i}": Driver(f"{prefix}{i}", lat, lng, seq_base) for i, (lat, lng) in enumerate(positions)}
        return cls(ds, phase={d: i % 8 for i, d in enumerate(ds)})

    def accept(self, driver_id: str, ride_id: str) -> bool:
        """True if the driver takes the ride now. Repeat offers of the same ride are ignored (False)."""
        d = self.drivers.get(driver_id)
        if d is None:
            return False
        if d.mode != IDLE:
            if d.ride_id != ride_id:
                self.conflicts += 1
            return False
        d.mode, d.ride_id = ROUTING, ride_id
        return True

    def start_leg(self, driver_id: str, leg: Leg) -> bool:
        """Start a leg, unless the ride was cancelled while its route was being fetched."""
        d = self.drivers[driver_id]
        if d.ride_id != leg.ride_id or d.mode != ROUTING:
            return False
        d.leg, d.mode = leg, (TO_PICKUP if leg.kind == "pickup" else ON_TRIP)
        return True

    def cancel(self, driver_id: str, ride_id: str) -> bool:
        """The offer lapsed before the accept landed: drop the ride where the driver stands."""
        d = self.drivers.get(driver_id)
        if d is None or d.ride_id != ride_id:
            return False
        d.mode, d.ride_id, d.leg = IDLE, None, None
        return True

    def advance(self, sim_t: float) -> list[tuple[str, Driver, Leg]]:
        """Move everyone to sim_t. Returns finished legs as ("pickup" | "trip", driver, leg)."""
        finished = []
        for d in self.drivers.values():
            if d.leg is None:
                continue
            d.lat, d.lng = d.leg.position(sim_t)
            if d.leg.done(sim_t):
                leg = d.leg
                d.leg = None
                if leg.kind == "pickup":
                    d.empty_m += leg.route.distance_m
                    d.mode = ROUTING                      # waiting for the trip route
                else:
                    d.loaded_m += leg.route.distance_m
                    d.trips += 1
                    d.mode, d.ride_id = IDLE, None
                finished.append((leg.kind, d, leg))
        return finished

    def due_pings(self, tick: int, every: int) -> list[Driver]:
        """Drivers whose ping slot is this tick (staggered so pings spread evenly)."""
        due = [d for d in self.drivers.values() if (tick + self.phase[d.id]) % every == 0]
        for d in due:
            d.seq += 1
        return due
