"""Demand forecasting and repositioning (M3): serve-time parity with training, the forecaster's cadence,
the repositioner's decisions, and the empty km a reposition costs."""
import json
from datetime import datetime

import h3
import lightgbm as lgb
import numpy as np
import pytest

from core import clock, config
from core.demand import LAST_SLOT, DemandModel, aligned_end, read_history, seed_history
from core.demand_features import DEMAND_CATEGORICAL, DEMAND_FEATURES, HISTORY, SLOTS_PER_WEEK, build
from core.osrm import Route
from sim.agents import IDLE, Fleet, Leg
from tests.helpers import now_ms
from workers.forecaster import SNAPSHOT, Forecaster
from workers.repositioner import MAX_MOVES, Repositioner, plan

MIDTOWN = h3.latlng_to_cell(40.7549, -73.9840, config.RES_DEMAND)


def _counts(n_hex=3, n_slots=HISTORY + 200, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(n_slots)
    base = 20 + 10 * np.sin(2 * np.pi * t / 96)
    return rng.poisson(base * np.arange(1, n_hex + 1)[:, None]).astype(np.float32)


@pytest.fixture(scope="module")
def tiny_model():
    H = _counts()
    hexes = [MIDTOWN] + list(h3.grid_ring(MIDTOWN, 1))[:2]
    codes = np.arange(len(hexes), dtype=np.float32)
    X = np.concatenate([build(H, t, np.datetime64(clock.slot_start(t)), codes) for t in range(HISTORY, H.shape[1])])
    y = np.concatenate([H[:, t] for t in range(HISTORY, H.shape[1])])
    booster = lgb.train({"objective": "poisson", "verbose": -1, "num_leaves": 7, "device_type": "cpu"},
                        lgb.Dataset(X, y, feature_name=DEMAND_FEATURES, categorical_feature=DEMAND_CATEGORICAL),
                        num_boost_round=20)
    return DemandModel("vtest", booster, hexes), H


def test_slots_share_the_training_epoch():
    assert clock.slot_of(datetime(2016, 1, 1, 0, 14)) == 0
    assert clock.slot_of(datetime(2016, 6, 7, 7, 0)) == (31 + 29 + 31 + 30 + 31 + 6) * 96 + 28
    assert clock.slot_start(clock.slot_of(datetime(2016, 6, 7, 7, 10))) == datetime(2016, 6, 7, 7, 0)


def test_aligned_end_keeps_the_hour_of_week():
    for live in (17_472, 17_473, 300_000, 300_001):
        end = aligned_end(live, 17_472)
        assert end <= 17_472 and end > 17_472 - SLOTS_PER_WEEK and (end - live) % SLOTS_PER_WEEK == 0


def test_seeded_history_reads_back_and_forecasts_like_training(r, tiny_model):
    model, H = tiny_model
    t = HISTORY + 50
    seed_history(r, H, model.hexes, data_end=t, live_last=t - 1)
    got = read_history(r, model.hexes)
    assert got is not None
    Hl, last = got
    assert last == t - 1 and np.array_equal(Hl, H[:, t - HISTORY:t])
    served = model.forecast(Hl, last + 1)
    trained = model.booster.predict(build(H, t, np.datetime64(clock.slot_start(t)), model.codes))
    assert np.allclose([served[h] for h in model.hexes], trained)          # no training/serving skew


def test_history_too_short_gives_no_forecast(r, tiny_model):
    model, _ = tiny_model
    r.set(LAST_SLOT, 100)
    r.rpush(f"demand_hist:{MIDTOWN}", *range(10))
    assert read_history(r, model.hexes) is None


class _Fixed:
    def __init__(self, model):
        self.model = model

    def current(self):
        return self.model


def test_forecaster_runs_once_per_closed_slot(r, tiny_model):
    model, H = tiny_model
    t = HISTORY + 10
    seed_history(r, H, model.hexes, data_end=t, live_last=t - 1)
    fc = Forecaster(r, _Fixed(model))
    out = fc.step()
    assert out["slot"] == t and set(out["hexes"]) == set(model.hexes)
    assert json.loads(r.get(SNAPSHOT))["slot"] == t
    assert fc.step() is None                                   # same slot: nothing new
    for h in model.hexes:
        r.rpush(f"demand_hist:{h}", 5)
    r.set(LAST_SLOT, t)                                        # the aggregator closed slot t
    assert fc.step()["slot"] == t + 1


# ---------------------------------------------------------------- repositioner
def _drivers(n, cell, prefix):
    lat, lng = h3.cell_to_latlng(cell)
    return [(f"{prefix}{i}", lat, lng) for i in range(n)]


def test_plan_moves_a_share_of_the_surplus_to_the_shortest_neighbour():
    short, shorter = list(h3.grid_ring(MIDTOWN, 1))[:2]
    need = {MIDTOWN: 2.0, short: 3.0, shorter: 9.0}
    idle = {MIDTOWN: _drivers(22, MIDTOWN, "a"), short: _drivers(1, short, "b")}
    moves = plan(need, idle)
    assert len(moves) == 4                                     # floor(0.2 x surplus 20)
    assert {(src, dst) for _, src, dst in moves} == {(MIDTOWN, shorter)}


def test_plan_never_overfills_the_target_or_moves_without_a_shortage():
    nb = list(h3.grid_ring(MIDTOWN, 1))[0]
    assert plan({MIDTOWN: 0.0, nb: 1.0}, {MIDTOWN: _drivers(30, MIDTOWN, "a"), nb: _drivers(5, nb, "b")}) == []
    moves = plan({MIDTOWN: 0.0, nb: 2.4}, {MIDTOWN: _drivers(30, MIDTOWN, "a")})
    assert len(moves) == 2                                     # the shortage, not the 6 the surplus allows
    assert plan({MIDTOWN: 0.0}, {MIDTOWN: _drivers(2, MIDTOWN, "a")}) == []   # surplus under the minimum


def test_plan_caps_moves_per_cycle():
    ring = list(h3.grid_ring(MIDTOWN, 2))
    need, idle = {}, {}
    for i, h in enumerate(ring):                               # many surplus hexes around a hungry centre
        idle[h] = _drivers(200, h, f"x{i}-")
    need[MIDTOWN] = 10_000.0
    for h in h3.grid_ring(MIDTOWN, 1):
        need[h] = 10_000.0
    assert len(plan(need, idle)) == MAX_MOVES


def test_repositioner_sends_idle_drivers_once_then_cools_down(r, s):
    nb = list(h3.grid_ring(MIDTOWN, 1))[0]
    t = now_ms()
    lat, lng = h3.cell_to_latlng(MIDTOWN)
    for i in range(15):
        cell = h3.latlng_to_cell(lat, lng, config.RES_DRIVER)
        s.move_driver(f"d{i}", cell, lat, lng, seq=1, ts_ms=t)
    r.set("forecast:snapshot", json.dumps({"slot": 1, "hexes": {MIDTOWN: 0.0, nb: 10.0}, "scale": 1.0}))
    rp = Repositioner(r)
    assert rp.step(now_ms=t) == []                              # off unless the run turns it on
    r.set("config:reposition", 1)
    p = r.pubsub()
    p.subscribe("ch:reposition")
    p.get_message(timeout=1)
    moves = rp.step(now_ms=t)
    assert len(moves) == 3 and all(m["to"] == nb for m in moves)  # floor(0.2 x surplus 15)
    assert h3.latlng_to_cell(moves[0]["lat"], moves[0]["lng"], config.RES_DEMAND) == nb
    msg = p.get_message(timeout=1)
    assert msg and len(json.loads(msg["data"])["moves"]) == 3
    assert [e for _, e in r.xrange("stream:events") if e["type"] == "reposition"]
    sent = {m["driver_id"] for m in moves}
    again = rp.step(now_ms=t + 1, force=True)                  # sent drivers are cooling down
    assert not sent & {m["driver_id"] for m in again}
    assert rp.step(now_ms=t + 2) == []                         # next cycle not due yet


def test_repositioner_cycle_survives_the_clock_going_back(r):
    rp = Repositioner(r)
    r.set("config:reposition", 1)
    r.set("forecast:snapshot", json.dumps({"slot": 1, "hexes": {}, "scale": 1.0}))
    t = now_ms()
    rp.step(now_ms=t)                                           # first cycle on the real clock (2026)
    assert rp.last_sim is not None
    clock.set_sim_clock(r, datetime(2016, 6, 7, 7), 10.0, wall0_ms=t)   # a replay starts: back to 2016
    rp.step(now_ms=t + 1000)
    assert rp.last_sim == clock.at(clock.read(r), t + 1000).timestamp()  # it ran instead of waiting ~10 years


# ---------------------------------------------------------------- empty km
def _route(n_m):
    lat0, lng = 40.7549, -73.9840
    dlat = n_m / 111_320
    return Route(duration_s=100.0, distance_m=n_m, coords=[(lat0, lng), (lat0 + dlat, lng)], cum=[0.0, n_m])


def test_reposition_counts_as_empty_km_even_when_cut_short():
    f = Fleet.at([(40.7549, -73.9840), (40.7549, -73.9840)], seq_base=0)
    assert f.reposition("rd0", Leg("reposition", "", _route(1000), 0.0, 100.0))
    f.advance(100.0)
    d = f.drivers["rd0"]
    assert d.mode == IDLE and d.empty_m == pytest.approx(1000) and d.repos_m == pytest.approx(1000)

    f.reposition("rd1", Leg("reposition", "", _route(1000), 0.0, 100.0))
    f.advance(40.0)                                            # 400 m in, an offer arrives
    assert f.accept("rd1", "r1")
    d = f.drivers["rd1"]
    assert d.empty_m == pytest.approx(400) and d.repos_m == pytest.approx(400) and d.leg is None
    assert not f.reposition("rd1", Leg("reposition", "", _route(500), 40.0, 50.0))   # busy: ignored
