"""The audit must report every real violation even with the live-safe recheck on (no false negatives)."""
import pytest

from tests.helpers import CELL_A, CELL_B, now_ms, queue_ride


@pytest.fixture
def t():
    return now_ms()


def _available(r, d, cell, t):
    r.hset(f"driver:{d}", mapping={"status": "available", "cell": cell, "ts": t, "seq": 1, "lat": 40.75, "lng": -73.98})


def _assigned(r, d, cell, ride, t):
    r.hset(f"driver:{d}", mapping={"status": "assigned", "cell": cell, "ts": t, "seq": 1, "ride": ride,
                                   "lat": 40.75, "lng": -73.98})


def test_clean_state_passes(r, s, t):
    s.move_driver("d1", CELL_A, 40.75, -73.98, seq=1, ts_ms=t)
    queue_ride(r, "r1")
    s.move_driver("d2", CELL_B, 40.75, -73.98, seq=1, ts_ms=t)
    s.claim_driver("d2", "r1", now_ms=t)
    from tools.audit import audit
    assert audit(r) == []


@pytest.mark.parametrize("recheck", [True, False])
def test_reports_every_real_violation(r, t, recheck):
    from tools.audit import audit

    _available(r, "two_cells", CELL_A, t)                         # C2: in two cells
    r.zadd(f"cell:{CELL_A}", {"two_cells": t})
    r.zadd(f"cell:{CELL_B}", {"two_cells": t})

    _available(r, "no_cell", CELL_A, t)                           # C2: fresh + available, unsearchable

    _available(r, "wrong_cell", CELL_A, t)                        # C2: indexed in the wrong cell
    r.zadd(f"cell:{CELL_B}", {"wrong_cell": t})

    _assigned(r, "busy_in_cell", CELL_A, "r1", t)                 # C2: assigned but still searchable
    r.set("lock:driver:busy_in_cell", "r1")
    r.hset("ride:r1", mapping={"status": "assigned", "driver": "busy_in_cell"})
    r.zadd(f"cell:{CELL_A}", {"busy_in_cell": t})

    _assigned(r, "lock_thief", CELL_A, "r2", t)                   # C1: lock owned by another ride
    r.set("lock:driver:lock_thief", "r3")
    r.hset("ride:r2", mapping={"status": "assigned", "driver": "lock_thief"})

    _assigned(r, "share1", CELL_A, "r4", t)                       # C1: two drivers on one ride
    _assigned(r, "share2", CELL_A, "r4", t)
    r.set("lock:driver:share1", "r4")
    r.set("lock:driver:share2", "r4")
    r.hset("ride:r4", mapping={"status": "assigned", "driver": "share1"})

    _assigned(r, "dbl", CELL_A, "r5", t)                          # C1: one driver named by two rides
    r.set("lock:driver:dbl", "r5")
    r.hset("ride:r5", mapping={"status": "assigned", "driver": "dbl"})
    r.hset("ride:r6", mapping={"status": "assigned", "driver": "dbl"})

    r.zadd(f"cell:{CELL_B}", {"ghost": t})                        # C2: cell entry with no driver hash

    errors = "\n".join(audit(r, recheck=recheck))
    for needle in [
        "two_cells should be only in",
        "no_cell should be only in", "wrong_cell should be only in",
        "assigned driver busy_in_cell still searchable",
        "lock_thief ride=r2 but lock owned by r3",
        "ride r4 booked to 2 drivers", "driver share2 holds ride r4 but ride.driver=share1",
        "driver dbl double-booked on rides ['r5', 'r6']",
        "unknown driver ghost",
    ]:
        assert needle in errors, f"missing: {needle}\n{errors}"
