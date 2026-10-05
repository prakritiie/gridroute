"""Simulator unit tests (no Redis)."""
import numpy as np

from core.config import LAT_MAX, LAT_MIN, LNG_MAX, LNG_MIN
from sim.world import M_PER_DEG_LAT, World


def _dist_m(lat1, lng1, lat2, lng2):
    dy = (lat2 - lat1) * M_PER_DEG_LAT
    dx = (lng2 - lng1) * M_PER_DEG_LAT * np.cos(np.radians(lat1))
    return np.hypot(dx, dy)


def test_drivers_stay_in_bbox():
    w = World(2000, seed=1)
    for _ in range(600):
        w.tick(5.0)
    assert ((LAT_MIN <= w.lat) & (w.lat <= LAT_MAX)).all()
    assert ((LNG_MIN <= w.lng) & (w.lng <= LNG_MAX)).all()


def test_tick_moves_at_speed_or_arrives():
    w = World(1000, seed=2)
    lat0, lng0 = w.lat.copy(), w.lng.copy()
    remaining = _dist_m(lat0, lng0, w.dest_lat, w.dest_lng)
    w.tick(1.0)
    moved = _dist_m(lat0, lng0, w.lat, w.lng)
    expected = np.minimum(w.speed, remaining)
    assert np.allclose(moved, expected, rtol=1e-3, atol=0.05)


def test_every_driver_pings_once_per_interval_with_increasing_seq():
    w = World(500, seed=3)
    counts = np.zeros(500, dtype=int)
    last_seq = np.zeros(500, dtype=int)
    for _ in range(40):                       # 10 intervals of 4 s
        w.tick()
        b = w.due_pings()
        assert (b.seq > last_seq[b.idx]).all()
        last_seq[b.idx] = b.seq
        counts[b.idx] += 1
    assert (counts == 10).all()
    assert (last_seq == 10).all()


def test_pings_are_staggered():
    w = World(4000, seed=4)
    sizes = []
    for _ in range(4):
        w.tick()
        sizes.append(len(w.due_pings()))
    assert sum(sizes) == 4000 and min(sizes) > 800      # ~1000 per tick, not 4000 every 4th tick


def test_killed_drivers_stop_pinging():
    w = World(1000, seed=5)
    dead = w.kill(0.3)
    assert len(dead) == 300
    for _ in range(8):
        w.tick()
        assert not np.isin(w.due_pings().idx, dead).any()


def test_deterministic_for_seed():
    a, b = World(300, seed=9), World(300, seed=9)
    for _ in range(20):
        a.tick(), b.tick()
    pa, pb = a.due_pings(), b.due_pings()
    assert np.array_equal(pa.idx, pb.idx) and np.array_equal(pa.lat, pb.lat) and np.array_equal(a.lng, b.lng)


def test_trip_lifecycle_roam_pickup_dropoff_done():
    from sim.world import ON_TRIP, ROAM, TO_PICKUP
    w = World(1, seed=6, speed_mps=(10.0, 10.0))
    pickup = (w.lat[0] + 0.0009, w.lng[0])                 # ~100 m away
    dropoff = (pickup[0] + 0.0018, pickup[1])              # ~200 m further
    assert w.assign("d0", "r1", pickup, dropoff)
    assert w.mode[0] == TO_PICKUP
    assert not w.assign("d0", "r1", pickup, dropoff)       # duplicate offer: not taken (or accepted) twice
    assert w.conflicts == 0
    assert not w.assign("d0", "r2", pickup, dropoff) and w.conflicts == 1

    done, ticks = [], 0
    while not done and ticks < 100:
        done = w.tick(1.0)
        ticks += 1
        if ticks == 11:
            assert w.mode[0] == ON_TRIP
    assert done == [("d0", "r1")] and 25 <= ticks <= 35
    assert w.mode[0] == ROAM and w.ride[0] is None


def test_dead_driver_ignores_assignments_and_stands_still():
    w = World(10, seed=7)
    w.alive[:] = False
    lat0 = w.lat.copy()
    assert not w.assign("d0", "r1", (40.75, -73.98))
    w.tick(5.0)
    assert np.array_equal(w.lat, lat0)
