"""Aggregator: supply/demand snapshot, rollups, KPIs, demand history, live audit."""
import json

import h3

from core import config
from core.scripts import Claim
from tests.helpers import CELL_A, CELL_B, now_ms, queue_ride
from workers.aggregator import SLOT_MS, Aggregator

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


def test_demand_history_rolls_every_15_minutes(r):
    slot0 = (now_ms() // SLOT_MS) * SLOT_MS
    agg = Aggregator(r, now_ms=slot0 + 1000)
    _ride_request(r, slot0 + 2000)
    _ride_request(r, slot0 + 3000)
    agg.step(now=slot0 + 4000)
    hex7 = h3.cell_to_parent(CELL_A, config.RES_DEMAND)
    assert not r.exists(f"demand_hist:{hex7}")            # slot still open

    _ride_request(r, slot0 + SLOT_MS + 1000)               # first request of the next slot
    agg.step(now=slot0 + SLOT_MS + 2000)
    assert r.lrange(f"demand_hist:{hex7}", 0, -1) == ["2"]

    agg.step(now=slot0 + 2 * SLOT_MS + 1000)
    assert r.lrange(f"demand_hist:{hex7}", 0, -1) == ["2", "1"]

    agg.step(now=slot0 + 3 * SLOT_MS + 1000)               # quiet slot still appends a 0
    assert r.lrange(f"demand_hist:{hex7}", 0, -1) == ["2", "1", "0"]


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
    agg.run_audit()
    assert agg.fleet["double_bookings"] == 1
