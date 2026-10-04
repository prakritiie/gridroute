"""move_driver.lua unit tests (spec §14: test_move_driver_seq, test_assigned_not_searchable, test_lease_expiry_revives)."""
import time

from core.scripts import Claim
from tests.helpers import CELL_A, CELL_B, now_ms, queue_ride
from tools.audit import audit


def cells_of(r, driver_id):
    return sorted(k.split(":", 1)[1] for k in r.scan_iter("cell:*") if r.zscore(k, driver_id) is not None)


def test_first_ping_indexes_driver(r, s):
    t = now_ms()
    assert s.move_driver("d1", CELL_A, 40.758, -73.985, seq=1, ts_ms=t)
    h = r.hgetall("driver:d1")
    assert h["status"] == "available" and h["cell"] == CELL_A and h["seq"] == "1"
    assert r.zscore(f"cell:{CELL_A}", "d1") == t          # score = freshness
    assert r.sismember("active_cells", CELL_A)
    assert 0 < r.pttl("driver:d1") <= 3_600_000
    assert audit(r) == []


def test_move_driver_seq(r, s):
    t = now_ms()
    assert s.move_driver("d1", CELL_A, 40.758, -73.985, seq=1, ts_ms=t)
    assert s.move_driver("d1", CELL_B, 40.759, -73.986, seq=2, ts_ms=t + 4000)
    assert cells_of(r, "d1") == [CELL_B]                 # moved: old cell cleaned up

    # Late and duplicate pings are ignored and change nothing
    assert not s.move_driver("d1", CELL_A, 40.758, -73.985, seq=1, ts_ms=t + 8000)
    assert not s.move_driver("d1", CELL_A, 40.758, -73.985, seq=2, ts_ms=t + 8000)
    h = r.hgetall("driver:d1")
    assert (h["cell"], h["seq"], h["ts"]) == (CELL_B, "2", str(t + 4000))
    assert cells_of(r, "d1") == [CELL_B]
    assert audit(r) == []


def test_same_cell_ping_refreshes_score(r, s):
    t = now_ms()
    s.move_driver("d1", CELL_A, 40.758, -73.985, seq=1, ts_ms=t - 10_000)
    s.move_driver("d1", CELL_A, 40.7581, -73.9851, seq=2, ts_ms=t)
    assert cells_of(r, "d1") == [CELL_A]
    assert r.zscore(f"cell:{CELL_A}", "d1") == t


def test_assigned_not_searchable(r, s):
    t = now_ms()
    s.move_driver("d1", CELL_A, 40.758, -73.985, seq=1, ts_ms=t)
    queue_ride(r, "r1")
    assert s.claim_driver("d1", "r1", now_ms=t) is Claim.CLAIMED
    assert cells_of(r, "d1") == []

    # Keeps driving while assigned: position tracked, but never re-enters a cell
    for seq, cell in [(2, CELL_B), (3, CELL_A), (4, CELL_B)]:
        assert s.move_driver("d1", cell, 40.76, -73.98, seq=seq, ts_ms=t + seq * 1000)
        assert cells_of(r, "d1") == []
    h = r.hgetall("driver:d1")
    assert (h["status"], h["ride"], h["cell"]) == ("assigned", "r1", CELL_B)
    assert audit(r) == []


def test_lease_expiry_revives(r, s):
    """trip_done lost: once the lock expires, the next ping makes the driver available again."""
    t = now_ms()
    s.move_driver("d1", CELL_A, 40.758, -73.985, seq=1, ts_ms=t)
    queue_ride(r, "r1")
    assert s.claim_driver("d1", "r1", now_ms=t, lease_ms=50) is Claim.CLAIMED

    time.sleep(0.15)
    assert not r.exists("lock:driver:d1")
    assert s.move_driver("d1", CELL_B, 40.759, -73.986, seq=2, ts_ms=now_ms())
    h = r.hgetall("driver:d1")
    assert h["status"] == "available" and "ride" not in h
    assert cells_of(r, "d1") == [CELL_B]
    assert audit(r) == []


def test_lease_expiry_closes_the_abandoned_ride(r, s):
    """Regression (found live): an offer lost while the gateway was down left ride r1 'assigned' forever.
    After the lease expired the driver was matched again, and two open rides named the same driver."""
    t = now_ms()
    s.move_driver("d1", CELL_A, 40.758, -73.985, seq=1, ts_ms=t)
    queue_ride(r, "r1")
    assert s.claim_driver("d1", "r1", now_ms=t, lease_ms=50) is Claim.CLAIMED
    time.sleep(0.15)                                      # the driver never heard about r1; lease runs out

    assert s.move_driver("d1", CELL_A, 40.758, -73.985, seq=2, ts_ms=now_ms())
    assert r.hget("ride:r1", "status") == "expired"
    ev = r.xrange("stream:events")[-1][1]
    assert (ev["type"], ev["ride"], ev["driver"]) == ("lease_expired", "r1", "d1")

    queue_ride(r, "r2")
    assert s.claim_driver("d1", "r2", now_ms=now_ms()) is Claim.CLAIMED
    assert audit(r) == []                                 # before the fix: "d1 double-booked on rides [r1, r2]"


def test_lease_expiry_leaves_rides_owned_by_others_alone(r, s):
    t = now_ms()
    s.move_driver("d1", CELL_A, 40.758, -73.985, seq=1, ts_ms=t)
    queue_ride(r, "r1")
    s.claim_driver("d1", "r1", now_ms=t, lease_ms=50)
    r.hset("ride:r1", mapping={"status": "completed"})     # already finished through another path
    time.sleep(0.15)
    s.move_driver("d1", CELL_A, 40.758, -73.985, seq=2, ts_ms=now_ms())
    assert r.hget("ride:r1", "status") == "completed"
    assert not r.exists("stream:events")
