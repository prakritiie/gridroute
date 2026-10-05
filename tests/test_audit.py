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
    r.sadd("rides:open", "r1", "r2", "r4", "r5", "r6")            # as claim_driver.lua would have indexed them

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


def test_open_ride_index_follows_every_transition(r, s, t):
    """rides:open is maintained by the Lua scripts in the same step as each status change."""
    import time as _t
    from workers.reaper import Reaper
    s.move_driver("d1", CELL_A, 40.75, -73.98, seq=1, ts_ms=t)
    queue_ride(r, "r1")
    s.claim_driver("d1", "r1", now_ms=t, state="offered")
    assert r.sismember("rides:open", "r1")                         # offered: open
    s.accept_offer("d1", "r1", now_ms())
    assert r.sismember("rides:open", "r1")                         # assigned: still open
    s.release_driver("d1", "r1", now_ms())
    assert not r.sismember("rides:open", "r1")                     # completed: closed

    queue_ride(r, "r2")
    s.claim_driver("d1", "r2", now_ms=now_ms(), lease_ms=50, state="offered")
    _t.sleep(0.15)
    Reaper(r).expire_offers(now_ms())
    assert not r.sismember("rides:open", "r2")                     # lapsed offer re-queued: closed

    queue_ride(r, "r3")
    s.claim_driver("d1", "r3", now_ms=now_ms(), lease_ms=50)       # whole-trip claim, lease runs out
    _t.sleep(0.15)
    s.move_driver("d1", CELL_A, 40.75, -73.98, seq=2, ts_ms=now_ms())
    assert r.hget("ride:r3", "status") == "expired" and not r.sismember("rides:open", "r3")
    from tools.audit import audit
    assert audit(r) == []


def test_audit_reports_index_drift(r, t):
    from tools.audit import audit
    r.hset("ride:old", mapping={"status": "completed", "driver": "d9"})
    r.sadd("rides:open", "old")
    assert any("rides:open lists ride old" in v for v in audit(r))


def test_audit_cost_does_not_grow_with_finished_rides(r, s, t):
    """Regression: the audit scanned every ride kept for 24 h (~7 s at 58k). Finished rides must cost nothing."""
    import time as _t
    from tools.audit import audit
    pipe = r.pipeline(transaction=False)
    for i in range(40_000):
        pipe.hset(f"ride:done{i}", mapping={"status": "completed", "driver": f"d{i % 50}"})
    pipe.execute()
    s.move_driver("d1", CELL_A, 40.75, -73.98, seq=1, ts_ms=t)
    t0 = _t.perf_counter()
    assert audit(r) == []
    assert _t.perf_counter() - t0 < 1.0
