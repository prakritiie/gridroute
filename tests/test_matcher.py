"""Greedy matcher (M1) against real Redis."""
import json
import time

import h3

from core import config
from core.scripts import Scripts
from tests.helpers import now_ms, queue_ride
from tools.audit import audit
from workers.matcher import EVENTS, GROUP, OFFERS, RIDES, Matcher

PICKUP = (40.7549, -73.9840)


def _driver(s, d, lat, lng, ts=None):
    s.move_driver(d, h3.latlng_to_cell(lat, lng, 9), lat, lng, seq=1, ts_ms=ts if ts is not None else now_ms())


def _request(r, ride_id, lat=PICKUP[0], lng=PICKUP[1], dropoff=None):
    queue_ride(r, ride_id, lat, lng)
    if dropoff:
        r.hset(f"ride:{ride_id}", mapping={"dlat": dropoff[0], "dlng": dropoff[1]})
    r.xadd(RIDES, {"ride_id": ride_id, "lat": lat, "lng": lng, "t": now_ms()})


def test_matches_nearest_driver_and_publishes_offer(r, s):
    _driver(s, "far", PICKUP[0] + 0.004, PICKUP[1])           # ~450 m
    _driver(s, "near", PICKUP[0] + 0.001, PICKUP[1])          # ~110 m
    m = Matcher(r, consumer="t")
    sub = r.pubsub()
    sub.subscribe(OFFERS)
    sub.get_message(timeout=1)                                 # subscribe confirmation

    _request(r, "r1", dropoff=(40.76, -73.97))
    assert m.step() == 1

    ride = r.hgetall("ride:r1")
    assert ride["status"] == "assigned" and ride["driver"] == "near"
    assert 90 < int(ride["dist_m"]) < 130 and int(ride["eta_pred"]) > 0
    offer = json.loads(sub.get_message(timeout=2)["data"])
    assert offer == {"type": "assignment", "driver_id": "near", "ride_id": "r1", "pickup": list(PICKUP),
                     "eta_s": int(ride["eta_pred"]), "dropoff": [40.76, -73.97]}
    ev = next(f for _, f in r.xrange(EVENTS) if f["type"] == "match")
    assert ev["driver"] == "near"
    assert any(f["type"] == "batch" and float(f["ms"]) >= 0 for _, f in r.xrange(EVENTS))
    assert r.xpending(RIDES, GROUP)["pending"] == 0
    assert audit(r) == []


def test_no_driver_requeues_then_unmatched(r):
    m = Matcher(r, consumer="t")
    _request(r, "r1")
    outcomes = []
    for _ in range(config.MAX_ATTEMPTS):
        resp = r.xreadgroup(GROUP, "t", {RIDES: ">"}, count=10)
        (entry_id, fields), = resp[0][1]
        outcomes.append(m.match_ride(fields["ride_id"]))
        r.xack(RIDES, GROUP, entry_id)
    assert outcomes == ["requeued"] * (config.MAX_ATTEMPTS - 1) + ["unmatched"]
    assert r.hget("ride:r1", "status") == "unmatched"
    assert r.xreadgroup(GROUP, "t", {RIDES: ">"}, count=10) == []    # not requeued again


def test_busy_driver_skipped(r, s):
    _driver(s, "near", PICKUP[0] + 0.001, PICKUP[1])
    _driver(s, "far", PICKUP[0] + 0.004, PICKUP[1])
    m = Matcher(r, consumer="t")
    _request(r, "r1")
    _request(r, "r2")
    m.step()
    assert r.hget("ride:r1", "driver") == "near"
    assert r.hget("ride:r2", "driver") == "far"
    assert audit(r) == []


def test_already_assigned_ride_is_republished_not_rematched(r, s):
    _driver(s, "d1", *PICKUP)
    _driver(s, "d2", *PICKUP)
    m = Matcher(r, consumer="t")
    _request(r, "r1")
    m.step()
    r.xadd(RIDES, {"ride_id": "r1", "lat": PICKUP[0], "lng": PICKUP[1], "t": now_ms()})   # duplicate entry
    assert m.step() == 1
    assert m.stats.already_handled == 1 and m.stats.matched == 1
    assert sum(r.hget(f"driver:{d}", "status") == "assigned" for d in ("d1", "d2")) == 1


def test_claim_lease_is_trip_lease(r, s):
    _driver(s, "d1", *PICKUP)
    m = Matcher(r, consumer="t")
    _request(r, "r1")
    m.step()
    assert config.OFFER_LEASE_MS < r.pttl("lock:driver:d1") <= config.TRIP_LEASE_MS


def test_reclaims_rides_from_dead_matcher(r, s):
    _driver(s, "d1", *PICKUP)
    Matcher(r, consumer="setup")
    _request(r, "r1")
    r.xreadgroup(GROUP, "dead", {RIDES: ">"}, count=10)        # read, then crash
    m = Matcher(r, consumer="alive", claim_idle_ms=0)
    time.sleep(0.01)
    assert m.reclaim() == 1
    assert r.hget("ride:r1", "status") == "assigned"
    assert r.xpending(RIDES, GROUP)["pending"] == 0
