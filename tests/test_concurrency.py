"""Concurrency tests with real OS processes (spec §14: test_no_double_booking, C1/C2).

The naive control proves the harness has teeth: if it stopped finding double-bookings,
a green test_no_double_booking would mean nothing.
"""
import multiprocessing as mp
import random
import time

import h3

from core.scripts import Scripts
from tests import race
from tests.helpers import CELL_A, TEST_REDIS_URL, now_ms, queue_ride
from tools.audit import audit

N_MATCHERS = 16


def _seed(r, n_drivers: int, n_rides: int) -> tuple[list[str], list[str]]:
    s = Scripts(r)
    t = now_ms()
    cells = list(h3.grid_disk(CELL_A, 3))
    drivers = [f"d{i}" for i in range(n_drivers)]
    for i, d in enumerate(drivers):
        s.move_driver(d, cells[i % len(cells)], 40.758, -73.985, seq=1, ts_ms=t)
    rides = [f"r{i}" for i in range(n_rides)]
    for ride in rides:
        queue_ride(r, ride)
    return drivers, rides


def _race(fn, rides, drivers) -> list[int]:
    start_at = time.time() + 2.0                           # let all spawned processes import first
    with mp.get_context("spawn").Pool(N_MATCHERS) as pool:
        return pool.starmap(fn, [(TEST_REDIS_URL, w, rides, drivers, start_at) for w in range(N_MATCHERS)])


def test_no_double_booking(r):
    """16 matchers fight over 200 drivers for 400 shared rides: every driver is claimed exactly once."""
    drivers, rides = _seed(r, n_drivers=200, n_rides=400)
    claimed = _race(race.lua_matcher, rides, drivers)

    assert sum(claimed) == len(drivers)                    # rides > drivers, so all drivers get taken, once
    assigned = [h for k in r.scan_iter("ride:*") if (h := r.hgetall(k))["status"] == "assigned"]
    assert len(assigned) == len(drivers)
    assert len({h["driver"] for h in assigned}) == len(drivers)
    for d in drivers:
        assert r.get(f"lock:driver:{d}") == r.hget(f"driver:{d}", "ride")
    assert not any(r.zcard(k) for k in r.scan_iter("cell:*"))   # nobody assigned is still searchable
    assert audit(r) == []


def test_no_double_booking_16_matcher_processes(r):
    """Week-2 gate: 16 real matcher workers drain a shared ride stream with 2x more rides than drivers,
    including duplicated stream entries. Every driver ends with at most one ride."""
    from core import config
    from workers.matcher import RIDES, Matcher

    drivers, rides = _seed(r, n_drivers=150, n_rides=300)
    Matcher(r, consumer="setup")                           # create the group
    pipe = r.pipeline(transaction=False)
    for ride in rides + rides[:60]:                        # 60 rides enqueued twice (gateway retry, replay)
        pipe.xadd(RIDES, {"ride_id": ride, "lat": 40.758, "lng": -73.9855, "t": now_ms()})
    pipe.execute()

    start_at = time.time() + 2.0
    with mp.get_context("spawn").Pool(N_MATCHERS) as pool:
        stats = pool.starmap(race.matcher_process, [(TEST_REDIS_URL, f"m{i}", start_at) for i in range(N_MATCHERS)])

    matched = sum(s["matched"] for s in stats)
    print(f"16 matchers: matched={matched} conflicts={sum(s['conflicts'] for s in stats)} "
          f"handled={sum(s['already_handled'] for s in stats)} unmatched={sum(s['unmatched'] for s in stats)} "
          f"busy workers={sum(s['rides'] > 0 for s in stats)}")
    statuses = [r.hget(f"ride:{x}", "status") for x in rides]
    assert matched == len(drivers) == statuses.count("offered")      # M2: matcher offers; accept is the driver's
    assert statuses.count("unmatched") == len(rides) - len(drivers)
    assert len({r.hget(f"ride:{x}", "driver") for x in rides if r.hget(f"ride:{x}", "status") == "offered"}) == 150
    # >= because a ride with two stream entries can have both copies counted in the same round
    assert all(int(r.hget(f"ride:{x}", "attempts")) >= config.MAX_ATTEMPTS
               for x in rides if r.hget(f"ride:{x}", "status") == "unmatched")
    assert audit(r) == []


def test_naive_claim_double_books(r):
    """Control: check-then-set without Lua double-books under the same harness, and the audit catches it."""
    drivers, rides = _seed(r, n_drivers=50, n_rides=100)
    claimed = _race(race.naive_matcher, rides, drivers)

    assert sum(claimed) > len(drivers)                     # more "successful" claims than drivers exist
    assert any(v.startswith("C1") for v in audit(r))


def test_parallel_moves_highest_seq_wins(r):
    """4 consumers apply shuffled pings for 100 drivers: the highest seq wins and each driver is in one cell."""
    cells = list(h3.grid_disk(CELL_A, 4))
    rng = random.Random(7)
    t0 = now_ms()
    pings, final = [], {}
    for i in range(100):
        d = f"d{i}"
        for seq in range(1, 31):
            cell = rng.choice(cells)
            pings.append((d, cell, 40.758, -73.985, seq, t0 + seq))
            final[d] = (cell, seq)
    rng.shuffle(pings)
    with mp.get_context("spawn").Pool(4) as pool:
        pool.starmap(race.ingest_consumer, [(TEST_REDIS_URL, pings[i::4]) for i in range(4)])

    for d, (cell, seq) in final.items():
        assert r.hmget(f"driver:{d}", "cell", "seq") == [cell, str(seq)]
    assert audit(r, now_ms=t0) == []
