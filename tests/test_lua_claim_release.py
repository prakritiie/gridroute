"""claim_driver.lua / release_driver.lua unit tests (spec §14: test_claim_idempotent, test_release_requires_owner)."""
from core.scripts import Claim
from tests.helpers import CELL_A, CELL_B, now_ms, queue_ride
from tools.audit import audit


def ping(s, d, cell=CELL_A, seq=1, ts=None):
    return s.move_driver(d, cell, 40.758, -73.985, seq=seq, ts_ms=ts if ts is not None else now_ms())


def test_claim_success(r, s):
    t = now_ms()
    ping(s, "d1", ts=t)
    queue_ride(r, "r1")
    assert s.claim_driver("d1", "r1", now_ms=t, lease_ms=15_000) is Claim.CLAIMED

    assert r.hmget("driver:d1", "status", "ride") == ["assigned", "r1"]
    assert r.get("lock:driver:d1") == "r1"
    assert 0 < r.pttl("lock:driver:d1") <= 15_000
    assert r.zscore(f"cell:{CELL_A}", "d1") is None
    assert r.hmget("ride:r1", "status", "driver", "t_assigned") == ["assigned", "d1", str(t)]
    assert audit(r) == []


def test_claim_idempotent(r, s):
    t = now_ms()
    ping(s, "d1", ts=t)
    ping(s, "d2", ts=t)
    queue_ride(r, "r1")
    assert s.claim_driver("d1", "r1", now_ms=t) is Claim.CLAIMED
    # A second matcher (or a retry) on the same ride gets -1 and must not touch any driver
    assert s.claim_driver("d2", "r1", now_ms=t) is Claim.RIDE_HANDLED
    assert s.claim_driver("d1", "r1", now_ms=t) is Claim.RIDE_HANDLED
    assert r.hget("driver:d2", "status") == "available" and not r.exists("lock:driver:d2")
    assert r.hget("ride:r1", "driver") == "d1"
    assert audit(r) == []


def test_claim_busy_driver(r, s):
    t = now_ms()
    ping(s, "d1", ts=t)
    queue_ride(r, "r1")
    queue_ride(r, "r2")
    assert s.claim_driver("d1", "r1", now_ms=t) is Claim.CLAIMED
    assert s.claim_driver("d1", "r2", now_ms=t) is Claim.DRIVER_TAKEN
    assert r.hget("ride:r2", "status") == "queued"          # r2 falls through to its next candidate
    assert r.get("lock:driver:d1") == "r1"


def test_claim_stale_driver(r, s):
    t = now_ms()
    ping(s, "d1", ts=t - 31_000)                               # silent > max_age
    queue_ride(r, "r1")
    assert s.claim_driver("d1", "r1", now_ms=t, max_age_ms=30_000) is Claim.DRIVER_TAKEN
    assert not r.exists("lock:driver:d1")
    assert r.hget("driver:d1", "status") == "available"


def test_claim_unknown_driver_or_ride(r, s):
    t = now_ms()
    ping(s, "d1", ts=t)
    assert s.claim_driver("d1", "nope", now_ms=t) is Claim.RIDE_HANDLED
    queue_ride(r, "r1")
    assert s.claim_driver("ghost", "r1", now_ms=t) is Claim.DRIVER_TAKEN
    assert not r.exists("driver:ghost") and not r.exists("lock:driver:ghost")


def test_release_requires_owner(r, s):
    t = now_ms()
    ping(s, "d1", ts=t)
    queue_ride(r, "r1")
    queue_ride(r, "r2")
    assert s.claim_driver("d1", "r1", now_ms=t) is Claim.CLAIMED

    assert not s.release_driver("d1", "r2", now_ms=t + 1)    # wrong ride can't release
    assert r.hget("driver:d1", "status") == "assigned"
    assert r.get("lock:driver:d1") == "r1"
    assert r.hget("ride:r2", "status") == "queued"

    assert s.release_driver("d1", "r1", now_ms=t + 2)
    assert r.hgetall("driver:d1").get("ride") is None
    assert r.hget("driver:d1", "status") == "available"
    assert not r.exists("lock:driver:d1")
    assert r.hmget("ride:r1", "status", "t_done") == ["completed", str(t + 2)]
    assert not s.release_driver("d1", "r1", now_ms=t + 3)    # double release is a no-op


def test_released_driver_is_searchable_immediately(r, s):
    t = now_ms()
    ping(s, "d1", ts=t)
    queue_ride(r, "r1")
    s.claim_driver("d1", "r1", now_ms=t)
    ping(s, "d1", cell=CELL_B, seq=2, ts=t + 4000)            # drove to the dropoff while assigned
    assert r.zscore(f"cell:{CELL_B}", "d1") is None
    assert s.release_driver("d1", "r1", now_ms=t + 5000)

    # Back in the cell of its last ping, scored by that ping's time, without waiting for the next one
    assert r.zscore(f"cell:{CELL_B}", "d1") == t + 4000 and CELL_B in r.smembers("active_cells")
    assert r.zscore(f"cell:{CELL_A}", "d1") is None
    assert audit(r, now_ms=t + 5000) == []
    queue_ride(r, "r2")
    assert s.claim_driver("d1", "r2", now_ms=t + 5000) is Claim.CLAIMED

    ping(s, "d1", cell=CELL_A, seq=3, ts=t + 8000)            # assigned again: still not searchable
    assert r.zscore(f"cell:{CELL_A}", "d1") is None and r.zscore(f"cell:{CELL_B}", "d1") is None


def test_release_of_silent_driver_is_reaped_normally(r, s):
    from workers.reaper import Reaper
    t = now_ms()
    ping(s, "d1", ts=t - 40_000)
    queue_ride(r, "r1")
    s.claim_driver("d1", "r1", now_ms=t - 40_000)
    assert s.release_driver("d1", "r1", now_ms=t)
    assert Reaper(r).step(now_ms=t) == 1                      # re-indexed with its old score -> evicted
    assert audit(r, now_ms=t) == []
