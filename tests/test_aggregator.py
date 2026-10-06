"""Aggregator: supply/demand snapshot, rollups, KPIs, demand history, live audit."""
import json
from datetime import datetime

import h3

from core import clock, config
from core.scripts import Claim
from tests.helpers import CELL_A, CELL_B, now_ms, queue_ride
from workers.aggregator import LAST_SLOT, Aggregator

LAT, LNG = h3.cell_to_latlng(CELL_A)


def _ride_request(r, t, lat=LAT, lng=LNG, retry=False):
    fields = {"ride_id": f"r{t}", "lat": lat, "lng": lng, "t": t}
    if retry:
        fields["retry"] = 1
    r.xadd("stream:rides", fields, id=f"{t}-*")


def _cells(snap, res):
    return {c: (s, d) for c, s, d in snap["res"][str(res)]}


def test_snapshot_counts_fresh_supply_and_recent_demand(r, s):
    t = now_ms()
    s.move_driver("d1", CELL_A, LAT, LNG, seq=1, ts_ms=t)
    s.move_driver("d2", CELL_A, LAT, LNG, seq=1, ts_ms=t)
    s.move_driver("stale", CELL_B, LAT, LNG, seq=1, ts_ms=t - 40_000)
    _ride_request(r, t - 6 * 60_000)                       # outside the 5-min window
    _ride_request(r, t - 3000, retry=True)                 # matcher retry: not new demand
    _ride_request(r, t - 2000)
    _ride_request(r, t - 1000)

    snap = Aggregator(r, now_ms=t - 7 * 60_000).step(now=t)
    res9 = _cells(snap, 9)
    assert res9 == {CELL_A: (2, 2)}                        # stale driver's cell is not supply
    assert json.loads(r.get("hexes:snapshot")) == snap


def test_rollup_to_parents(r, s):
    t = now_ms()
    s.move_driver("d1", CELL_A, LAT, LNG, seq=1, ts_ms=t)
    lat_b, lng_b = h3.cell_to_latlng(CELL_B)
    s.move_driver("d2", CELL_B, lat_b, lng_b, seq=1, ts_ms=t)
    _ride_request(r, t - 500, lat_b, lng_b)
    snap = Aggregator(r, now_ms=t).step(now=t)
    for res in (8, 7):
        total_s = sum(v[0] for v in _cells(snap, res).values())
        total_d = sum(v[1] for v in _cells(snap, res).values())
        assert (total_s, total_d) == (2, 1)
        assert set(_cells(snap, res)) <= {h3.cell_to_parent(CELL_A, res), h3.cell_to_parent(CELL_B, res)}


def test_kpis_from_events(r, s):
    t = now_ms()
    s.move_driver("d1", CELL_A, LAT, LNG, seq=1, ts_ms=t)
    for i, (kind, eta) in enumerate([("match", 100), ("match", 300), ("unmatched", 0), ("complete", 0)]):
        r.xadd("stream:events", {"type": kind, "eta_s": eta, "t": t}, id=f"{t - 10_000 + i}-*")
    _ride_request(r, t - 5000)
    agg = Aggregator(r, now_ms=t - 60_000)
    agg.step(now=t)
    k = json.loads(r.get("kpis:latest"))
    assert k["drivers_available"] == 1 and k["requests_per_min"] == 1
    assert (k["matched_per_min"], k["unmatched_per_min"], k["completed_per_min"]) == (2, 1, 1)
    assert k["match_rate_5m"] == round(2 / 3, 4) and k["eta_mean_s"] == 200
    assert k["double_bookings"] == 0


SIM0 = datetime(2016, 6, 7, 7, 0)
SLOT_WALL = 90_000                                          # a 15-min sim slot at x10 is 90 s of wall time


def _sim(r, speed=10.0):
    w0 = now_ms()
    clock.set_sim_clock(r, SIM0, speed, wall0_ms=w0)
    return w0


def test_demand_history_rolls_on_sim_clock_slots(r):
    w0 = _sim(r)
    hex7 = h3.cell_to_parent(CELL_A, config.RES_DEMAND)
    agg = Aggregator(r, now_ms=w0)
    _ride_request(r, w0 + 2000)
    _ride_request(r, w0 + 3000)
    agg.step(now=w0 + 4000)
    assert not r.exists(f"demand_hist:{hex7}")              # slot still open
    assert int(r.get(LAST_SLOT)) == clock.slot_of(SIM0) - 1

    _ride_request(r, w0 + SLOT_WALL + 1000)                 # first request of the next sim slot
    agg.step(now=w0 + SLOT_WALL + 2000)
    assert r.lrange(f"demand_hist:{hex7}", 0, -1) == ["2"]
    agg.step(now=w0 + 2 * SLOT_WALL + 1000)
    assert r.lrange(f"demand_hist:{hex7}", 0, -1) == ["2", "1"]
    agg.step(now=w0 + 4 * SLOT_WALL + 1000)                 # two quiet slots at once: both appended as 0
    assert r.lrange(f"demand_hist:{hex7}", 0, -1) == ["2", "1", "0", "0"]
    assert int(r.get(LAST_SLOT)) == clock.slot_of(SIM0) + 3


def test_demand_history_is_scaled_to_full_city_units(r):
    w0 = _sim(r)
    r.set("config:demand_scale", 0.2)                       # a 20% replay
    hex7 = h3.cell_to_parent(CELL_A, config.RES_DEMAND)
    agg = Aggregator(r, now_ms=w0)
    agg.step(now=w0 + 1000)
    for i in range(3):
        _ride_request(r, w0 + 2000 + i)
    agg.step(now=w0 + SLOT_WALL + 1000)
    assert r.lrange(f"demand_hist:{hex7}", 0, -1) == ["15"]


def test_demand_history_restarts_after_a_clock_jump(r):
    w0 = _sim(r)
    hex7 = h3.cell_to_parent(CELL_A, config.RES_DEMAND)
    agg = Aggregator(r, now_ms=w0)
    agg.step(now=w0 + 1000)
    _ride_request(r, w0 + 2000)
    agg.step(now=w0 + SLOT_WALL + 1000)
    assert r.lrange(f"demand_hist:{hex7}", 0, -1) == ["1"]

    agg.step(now=w0 + 20 * SLOT_WALL)                      # a long pause (laptop sleep): no padded zeros
    assert not r.exists(f"demand_hist:{hex7}")
    assert int(r.get(LAST_SLOT)) == clock.slot_of(SIM0) + 19

    r.set(LAST_SLOT, clock.slot_of(SIM0) + 500)             # history from a later timeline: clock went back
    r.rpush(f"demand_hist:{hex7}", 9)
    agg.step(now=w0 + 20 * SLOT_WALL + 1000)
    assert not r.exists(f"demand_hist:{hex7}")
    assert int(r.get(LAST_SLOT)) == clock.slot_of(SIM0) + 19


def test_warm_started_history_is_continued(r):
    w0 = _sim(r)
    hex7 = h3.cell_to_parent(CELL_A, config.RES_DEMAND)
    r.rpush(f"demand_hist:{hex7}", 4, 5)                    # what a replay seeds from the data
    r.set(LAST_SLOT, clock.slot_of(SIM0) - 1)
    agg = Aggregator(r, now_ms=w0)
    _ride_request(r, w0 + 1000)
    agg.step(now=w0 + 2000)
    agg.step(now=w0 + SLOT_WALL + 1000)
    assert r.lrange(f"demand_hist:{hex7}", 0, -1) == ["4", "5", "1"]


def test_audit_feeds_fleet_kpis(r, s):
    t = now_ms()
    s.move_driver("d1", CELL_A, LAT, LNG, seq=1, ts_ms=t)
    s.move_driver("d2", CELL_A, LAT, LNG, seq=1, ts_ms=t)
    queue_ride(r, "r1")
    assert s.claim_driver("d1", "r1", now_ms=t) is Claim.CLAIMED
    agg = Aggregator(r)
    agg.run_audit()
    assert agg.fleet["busy"] == 1 and agg.fleet["online"] == 2 and agg.fleet["double_bookings"] == 0

    r.hset("ride:r2", mapping={"status": "assigned", "driver": "d1"})      # plant a double-booking
    r.sadd("rides:open", "r2")
    agg.run_audit()
    assert agg.fleet["double_bookings"] == 1
