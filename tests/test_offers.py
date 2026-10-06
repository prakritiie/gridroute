"""Offer/accept with short leases (M2): queued -> offered -> assigned, or back to queued if the offer lapses."""
import time

import pytest
from fastapi.testclient import TestClient

from core import config
from core.scripts import Claim
from tests.helpers import CELL_A, TEST_REDIS_URL, now_ms, queue_ride
from tools.audit import audit
from workers.matcher import RIDES, Matcher
from workers.reaper import Reaper

LAT, LNG = 40.7549, -73.9840


def _driver(s, d, ts=None, seq=1):
    s.move_driver(d, CELL_A, LAT, LNG, seq=seq, ts_ms=ts if ts is not None else now_ms())


def _offer(s, d, ride, lease_ms=15_000):
    return s.claim_driver(d, ride, now_ms=now_ms(), lease_ms=lease_ms, state="offered")


def test_accept_within_lease_turns_offer_into_trip(r, s):
    _driver(s, "d1")
    queue_ride(r, "r1")
    assert _offer(s, "d1", "r1") is Claim.CLAIMED
    assert r.hget("ride:r1", "status") == "offered" and r.zscore("offers", "r1") is not None
    assert audit(r) == []                                          # an offer is an open ride for C1

    assert s.accept_offer("d1", "r1", now_ms()) == 1
    assert r.hmget("ride:r1", "status", "driver") == ["assigned", "d1"]
    assert r.pttl("lock:driver:d1") > config.OFFER_LEASE_MS          # extended to the trip lease
    assert r.zscore("offers", "r1") is None
    assert s.accept_offer("d1", "r1", now_ms()) == -1              # a second accept changes nothing
    assert audit(r) == []


def test_only_the_offered_driver_can_accept(r, s):
    _driver(s, "d1")
    _driver(s, "d2")
    queue_ride(r, "r1")
    _offer(s, "d1", "r1")
    assert s.accept_offer("d2", "r1", now_ms()) == 0
    assert r.hget("ride:r1", "status") == "offered"


def test_lapsed_offer_is_requeued_by_the_reaper(r, s):
    _driver(s, "d1")
    queue_ride(r, "r1")
    _offer(s, "d1", "r1", lease_ms=50)
    time.sleep(0.15)

    assert s.accept_offer("d1", "r1", now_ms()) == 0               # too late: the driver hears "lapsed"
    reaper = Reaper(r)
    reaper.step()
    assert reaper.last_requeued == 1
    ride = r.hgetall("ride:r1")
    assert ride["status"] == "queued" and "driver" not in ride and ride["attempts"] == "1"
    (_, entry), = r.xrange(RIDES)
    assert entry["ride_id"] == "r1" and entry["retry"] == "1"
    assert r.hget("driver:d1", "status") == "available"
    assert r.zscore(f"cell:{CELL_A}", "d1") is not None             # searchable again at once
    assert r.zscore("offers", "r1") is None
    assert any(f["type"] == "offer_expired" for _, f in r.xrange("stream:events"))
    assert audit(r) == []
    assert _offer(s, "d1", "r1") is Claim.CLAIMED                  # and can be offered again


def test_reaper_leaves_offers_inside_their_lease(r, s):
    _driver(s, "d1")
    queue_ride(r, "r1")
    _offer(s, "d1", "r1", lease_ms=15_000)
    assert Reaper(r).expire_offers(now_ms() + 20_000) == 0         # zset says lapsed, but the lock still holds
    assert r.hget("ride:r1", "status") == "offered"


def test_driver_ping_after_lapse_requeues_too(r, s):
    """Whichever runs first, move_driver.lua or the reaper, the ride goes back to the queue exactly once."""
    _driver(s, "d1", seq=1)
    queue_ride(r, "r1")
    _offer(s, "d1", "r1", lease_ms=50)
    time.sleep(0.15)
    _driver(s, "d1", seq=2)
    assert r.hget("ride:r1", "status") == "queued" and r.hget("driver:d1", "status") == "available"
    assert Reaper(r).expire_offers(now_ms()) == 0
    assert len(r.xrange(RIDES)) == 1                                # one re-queue, not two
    assert audit(r) == []


def test_lost_offers_end_to_end(r, s):
    """The bug found live: offers published while nobody listened. Now they lapse, get re-queued, re-offered
    to another driver, accepted, and nothing is stranded."""
    _driver(s, "d1")
    _driver(s, "d2")
    for i in range(2):
        queue_ride(r, f"r{i}")
        r.xadd(RIDES, {"ride_id": f"r{i}", "lat": LAT, "lng": LNG, "t": now_ms()})
    m = Matcher(r, consumer="t", lease_ms=50)
    m.step()
    assert {r.hget(f"ride:r{i}", "status") for i in range(2)} == {"offered"}

    time.sleep(0.15)                                                # nobody accepted: the offers were lost
    Reaper(r).step()
    assert {r.hget(f"ride:r{i}", "status") for i in range(2)} == {"queued"}

    m.lease_ms = config.OFFER_LEASE_MS
    m.step()                                                        # re-offered from the retry entries
    for i in range(2):
        d = r.hget(f"ride:r{i}", "driver")
        assert s.accept_offer(d, f"r{i}", now_ms()) == 1
    assert {r.hget(f"ride:r{i}", "status") for i in range(2)} == {"assigned"}
    assert audit(r) == []


@pytest.fixture
def client(r, monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", TEST_REDIS_URL)
    from gateway.app import app
    with TestClient(app) as c:
        yield c


def test_gateway_accept_message(client, r, s):
    _driver(s, "d1")
    queue_ride(r, "r1")
    _offer(s, "d1", "r1")
    with client.websocket_connect("/ws/driver") as ws:
        ws.send_json({"type": "accept", "driver_id": "d1", "ride_id": "r1"})
        assert ws.receive_json() == {"type": "accept_ack", "ride_id": "r1", "driver_id": "d1", "ok": True}
        ws.send_json({"type": "accept", "driver_id": "d1", "ride_id": "r1"})
        assert ws.receive_json()["ok"] is False
    assert r.hget("ride:r1", "status") == "assigned"
    assert any(f["type"] == "accept" for _, f in r.xrange("stream:events"))


def test_sweep_closes_rides_of_drivers_that_vanished(r, s):
    """Regression (found live after a 16 h laptop sleep): driver hashes expire after 1 h of silence, the next
    ping recreates the driver as fresh and available, and its accepted ride stayed 'assigned' forever."""
    _driver(s, "d1")
    _driver(s, "d2")
    queue_ride(r, "trip")
    queue_ride(r, "offer")
    _offer(s, "d1", "trip")
    assert s.accept_offer("d1", "trip", now_ms()) == 1
    _offer(s, "d2", "offer")
    # both drivers go silent long enough for their hashes and locks to expire
    r.delete("driver:d1", "lock:driver:d1", "driver:d2", "lock:driver:d2")
    reaper = Reaper(r)
    reaper.step()
    assert reaper.last_swept == 2
    assert r.hget("ride:trip", "status") == "expired"
    assert r.hget("ride:offer", "status") == "queued"                  # an offer goes back to the queue
    assert not r.smembers("rides:open")
    _driver(s, "d1", seq=99)                                            # the driver comes back later
    assert r.hget("driver:d1", "status") == "available" and audit(r) == []


def test_sweep_leaves_healthy_rides_alone(r, s):
    _driver(s, "d1")
    queue_ride(r, "r1")
    _offer(s, "d1", "r1")
    s.accept_offer("d1", "r1", now_ms())
    reaper = Reaper(r)
    reaper.step()
    assert reaper.last_swept == 0 and r.hget("ride:r1", "status") == "assigned"
