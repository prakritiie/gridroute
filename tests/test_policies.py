"""Matching policies (spec §8.4 R0-R3): greedy vs Hungarian batch, distance vs ETA cost, B3 fallback, explain."""
import json
import time
from datetime import datetime

import h3
import numpy as np
import pytest
from fastapi.testclient import TestClient

from core import clock, config, osrm
from tests.helpers import TEST_REDIS_URL, now_ms, queue_ride
from tools.audit import audit
from workers.matcher import RIDES, Matcher

LAT0, LNG = 40.7549, -73.9840
M = 1 / 111_320                                   # degrees of latitude per metre


def _driver(s, d, north_m):
    lat = LAT0 + north_m * M
    s.move_driver(d, h3.latlng_to_cell(lat, LNG, 9), lat, LNG, seq=1, ts_ms=now_ms())


def _ride(r, rid, north_m):
    queue_ride(r, rid, LAT0 + north_m * M, LNG)
    r.xadd(RIDES, {"ride_id": rid, "lat": LAT0 + north_m * M, "lng": LNG, "t": now_ms()})


def _scenario(r, s):
    """Greedy-by-arrival gives ride 1 the shared nearest driver and strands ride 2 with the far one."""
    _driver(s, "A", 100)
    _driver(s, "B", -150)
    _ride(r, "r1", 0)        # A is 100 m away, B 150 m
    _ride(r, "r2", 190)      # A is 90 m away, B 340 m


def _total_m(r):
    pos = {"A": 100, "B": -150, "r1": 0, "r2": 190}
    return sum(abs(pos[rid] - pos[r.hget(f"ride:{rid}", "driver")]) for rid in ("r1", "r2"))


def _run(r, policy):
    r.set("config:policy", policy)
    m = Matcher(r, consumer="t")
    m.step()
    return m


def test_greedy_distance_takes_rides_in_arrival_order(r, s):
    _scenario(r, s)
    _run(r, "greedy-distance")
    assert r.hget("ride:r1", "driver") == "A" and r.hget("ride:r2", "driver") == "B"
    assert _total_m(r) == pytest.approx(440, abs=3)


def test_batch_distance_minimises_the_total(r, s):
    _scenario(r, s)
    _run(r, "batch-distance")
    assert r.hget("ride:r1", "driver") == "B" and r.hget("ride:r2", "driver") == "A"
    assert _total_m(r) == pytest.approx(240, abs=3)              # Hungarian: 240 m instead of 440 m
    assert audit(r) == []


def test_explain_records_candidates_and_choice(r, s):
    _scenario(r, s)
    _run(r, "batch-distance")
    ex = json.loads(r.get("explain:r1"))
    assert ex["policy"] == "batch-distance" and ex["chosen"] == "B" and ex["cost_unit"] == "m"
    assert {c["driver"] for c in ex["candidates"]} == {"A", "B"}
    assert r.ttl("explain:r1") > 0


class FakeModel:
    """Predicts 2x the OSRM time (so ETA cost can disagree with distance); B3 fallback = 1 s per metre."""
    version = "vtest"

    def predict(self, plat, plng, dlat, dlng, ts, osrm_s, osrm_m):
        return 2.0 * np.asarray(osrm_s)

    def fallback(self, plat, plng, how, straight_m):
        return np.asarray(straight_m, dtype=float)


def test_eta_cost_uses_the_model(r, s, monkeypatch):
    _scenario(r, s)
    # OSRM says driver B reaches anything in 30 s and A takes 200 s (say A is across a one-way maze)
    def fake_table(sources, destinations, **_):
        dur = [[200.0 if abs(src[0] - (LAT0 + 100 * M)) < 1e-9 else 30.0 for _ in destinations] for src in sources]
        return dur, [[500.0 for _ in destinations] for _ in sources]
    monkeypatch.setattr(osrm, "table", fake_table)
    m = Matcher(r, consumer="t")
    monkeypatch.setattr(m.registry, "current", lambda: FakeModel())
    r.set("config:policy", "greedy-eta")
    m.step()
    # by distance r1 would take A; by predicted time B (60 s) beats A (400 s)
    assert r.hget("ride:r1", "driver") == "B"
    assert r.hget("ride:r1", "eta_pred") == "60" and r.hget("ride:r1", "eta_model") == "vtest"
    ex = json.loads(r.get("explain:r1"))
    assert ex["cost_unit"] == "s" and ex["fallback"] is False
    b = next(c for c in ex["candidates"] if c["driver"] == "B")
    assert (b["osrm_s"], b["model_s"], b["cost"]) == (30.0, 60.0, 60.0)


def test_osrm_timeout_falls_back_to_b3(r, s, monkeypatch):
    _scenario(r, s)
    monkeypatch.setattr(osrm, "table", lambda *a, **k: None)     # OSRM missed its 150 ms budget
    m = Matcher(r, consumer="t")
    monkeypatch.setattr(m.registry, "current", lambda: FakeModel())
    r.set("config:policy", "batch-eta")
    m.step()
    assert {r.hget(f"ride:{x}", "status") for x in ("r1", "r2")} == {"offered"}   # still dispatched
    assert r.hget("ride:r1", "eta_fallback") == "1"
    assert json.loads(r.get("explain:r1"))["fallback"] is True


def test_unknown_policy_falls_back_to_r0(r, s):
    _scenario(r, s)
    m = _run(r, "something-else")
    assert m.policy == "greedy-distance" and r.hget("ride:r1", "driver") == "A"


def test_sim_clock_runs_at_replay_speed(r):
    clock.set_sim_clock(r, datetime(2016, 6, 7, 7, 0), speed=10.0, wall0_ms=int(time.time() * 1000) - 6000)
    t = clock.now(r)
    assert datetime(2016, 6, 7, 7, 0, 59) <= t <= datetime(2016, 6, 7, 7, 1, 2)   # 6 wall-s x10 = 60 sim-s
    r.delete(clock.KEY)
    assert clock.now(r).year >= 2026                                # back to real NYC time


def test_gateway_explain_endpoint(r, monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", TEST_REDIS_URL)
    from gateway.app import app
    r.set("explain:r9", json.dumps({"ride": "r9", "chosen": "d1", "candidates": []}))
    with TestClient(app) as c:
        assert c.get("/rides/r9/explain").json()["chosen"] == "d1"
        assert c.get("/rides/nope/explain").status_code == 404


def test_reset_keeps_model_pointer_and_consumer_groups(r, s):
    """Regression: replay --reset used FLUSHDB, wiping model:eta:current (a whole replay ran on the B3 fallback)
    and the consumer groups (workers crashed on NOGROUP)."""
    from tools.reset import reset_sim_state
    from workers.ingest import Ingest
    r.set("model:eta:current", "v1")
    Matcher(r, consumer="t")
    Ingest(r, consumer="t")
    _scenario(r, s)
    reset_sim_state(r)
    assert r.get("model:eta:current") == "v1"
    assert not list(r.scan_iter("driver:*")) and not list(r.scan_iter("ride:*")) and not r.exists("rides:open")
    groups = {s_: [g["name"] for g in r.xinfo_groups(s_)] for s_ in ("stream:rides", "stream:pings")}
    assert groups == {"stream:rides": ["match"], "stream:pings": ["ingest"]} and r.xlen("stream:rides") == 0


def test_workers_survive_a_deleted_stream(r):
    m = Matcher(r, consumer="t")
    r.delete("stream:rides")                       # e.g. someone runs FLUSHDB anyway
    assert m.step() == 0                           # no NOGROUP crash
    assert [g["name"] for g in r.xinfo_groups("stream:rides")] == ["match"]
