"""Replay pieces: street-following agents, congestion lookup, deterministic inputs, recorder -> report."""
import json
from datetime import datetime, timedelta

import duckdb
import h3
import numpy as np
import pytest

from core import config
from core.osrm import Route
from data.congestion import build
from eval.report import run_metrics
from sim.agents import IDLE, ON_TRIP, ROUTING, TO_PICKUP, Fleet, Leg
from sim.congestion import Congestion, hour_of_week
from sim.replay import load_window
from workers.recorder import record


# ---------------------------------------------------------------- agents
def _route(n_m=1000.0):
    # due north from Midtown: 3 points, 0 / 500 / 1000 m
    lat0, lng = 40.7549, -73.9840
    dlat = n_m / 111_320
    return Route(duration_s=100.0, distance_m=n_m, coords=[(lat0, lng), (lat0 + dlat / 2, lng), (lat0 + dlat, lng)],
                 cum=[0.0, n_m / 2, n_m])


def test_leg_interpolates_along_the_road():
    leg = Leg("pickup", "r1", _route(), start_sim=10.0, duration_s=200.0)
    assert leg.position(10.0) == leg.route.coords[0]
    mid = leg.position(110.0)
    assert mid[0] == pytest.approx(leg.route.coords[1][0], abs=1e-9)
    assert leg.position(500.0) == pytest.approx(leg.route.coords[2])
    assert not leg.done(209.0) and leg.done(210.0)


def test_fleet_lifecycle_and_km_accounting():
    f = Fleet.at([(40.75, -73.98), (40.76, -73.97)], seq_base=1000)
    assert f.accept("rd0", "r1")
    assert not f.accept("rd0", "r1") and f.conflicts == 0      # repeat of the same offer: ignored
    assert not f.accept("rd0", "r2") and f.conflicts == 1      # a different ride for a busy driver
    assert f.drivers["rd0"].mode == ROUTING

    f.start_leg("rd0", Leg("pickup", "r1", _route(800), 0.0, 60.0))
    assert f.drivers["rd0"].mode == TO_PICKUP and f.advance(30.0) == []
    [(kind, d, _)] = f.advance(60.0)
    assert kind == "pickup" and d.empty_m == 800 and d.mode == ROUTING

    f.start_leg("rd0", Leg("trip", "r1", _route(2500), 60.0, 600.0))
    assert f.drivers["rd0"].mode == ON_TRIP
    [(kind, d, _)] = f.advance(700.0)
    assert kind == "trip" and d.loaded_m == 2500 and d.trips == 1 and d.mode == IDLE and d.ride_id is None


def test_pings_are_staggered_and_seq_grows():
    f = Fleet.at([(40.75, -73.98)] * 16, seq_base=5000)
    seen = []
    for tick in range(8):
        seen += [d.id for d in f.due_pings(tick, 8)]
    assert sorted(seen) == sorted(f.drivers) and all(d.seq == 5001 for d in f.drivers.values())


# ---------------------------------------------------------------- congestion
def test_congestion_falls_back_from_hex_to_area_to_hour_to_global():
    lat, lng = 40.7549, -73.9840
    r8 = h3.latlng_to_cell(lat, lng, config.RES_ETA)
    r7 = h3.cell_to_parent(r8, config.RES_DEMAND)
    c = Congestion([("res8", r8, 10, 2.5, 30), ("res7", r7, 11, 2.0, 50), ("how", "", 12, 1.6, 900),
                    ("global", "", -1, 1.8, 10_000)])
    assert c.factor(lat, lng, 10) == 2.5
    assert c.factor(lat, lng, 11) == 2.0
    assert c.factor(lat, lng, 12) == 1.6
    assert c.factor(lat, lng, 99) == 1.8
    assert hour_of_week(datetime(2016, 6, 7, 8)) == 1 * 24 + 8             # Tuesday 08:00


def test_congestion_build_uses_medians_and_minimum_counts():
    n = 50
    trips = np.zeros(n, dtype=[("lat", float), ("lng", float), ("how", int), ("actual_s", float)])
    trips["lat"], trips["lng"], trips["how"] = 40.7549, -73.9840, 32
    trips["actual_s"] = np.r_[np.full(30, 200.0), np.full(20, 400.0)]       # ratios 2.0 x30, 4.0 x20
    rows = build(duckdb.connect(), trips, np.full(n, 100.0))
    by = {(lvl, how): f for lvl, _cell, how, f, _n in rows}
    assert by[("res8", 32)] == 2.0 and by[("how", 32)] == 2.0 and by[("global", -1)] == 2.0


# ---------------------------------------------------------------- deterministic inputs
@pytest.fixture
def june(tmp_path):
    rng = np.random.default_rng(0)
    t0 = datetime(2016, 6, 7, 5, 0)
    rows = []
    for i in range(4000):
        ts = t0 + timedelta(seconds=int(rng.integers(0, 5 * 3600)))
        rows.append((ts, ts + timedelta(minutes=12), 40.75 + rng.normal(0, 0.01), -73.98 + rng.normal(0, 0.01),
                     40.76 + rng.normal(0, 0.01), -73.97 + rng.normal(0, 0.01), 720))
    con = duckdb.connect()
    con.execute("""CREATE TABLE t (pickup_ts TIMESTAMP, dropoff_ts TIMESTAMP, pickup_lat DOUBLE, pickup_lng DOUBLE,
                                   dropoff_lat DOUBLE, dropoff_lng DOUBLE, duration_s INTEGER)""")
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    (tmp_path / "clean" / "trips").mkdir(parents=True)
    con.execute(f"COPY t TO '{(tmp_path / 'clean' / 'trips' / '2016-06.parquet').as_posix()}' (FORMAT parquet)")
    return tmp_path


def test_replay_inputs_are_deterministic_per_seed(june):
    a = load_window(june, "2016-06-07", "07:00", 2, 0.2, 100, seed=1)
    b = load_window(june, "2016-06-07", "07:00", 2, 0.2, 100, seed=1)
    c = load_window(june, "2016-06-07", "07:00", 2, 0.2, 100, seed=2)
    assert a[2] == b[2] and a[3] == b[3]                    # same requests, same fleet
    assert a[2] != c[2] and a[3] != c[3]
    assert all(datetime(2016, 6, 7, 7) <= t[0] < datetime(2016, 6, 7, 9) for t in a[2])
    assert 0.1 < len(a[2]) / 1600 < 0.3                     # ~1,600 trips in the window, 20% sampled
    assert len(a[3]) == 100


# ---------------------------------------------------------------- recorder -> report
def test_recorder_and_report_compute_the_row(r, tmp_path):
    start_id = "1000-0"
    ev = [("match", {"ride": "a"}), ("batch", {"ms": 10}), ("batch", {"ms": 50}),
          ("pickup", {"ride": "a", "leg_s": 300, "osrm_s": 150, "wait_s": 360, "match_s": 20}),
          ("pickup", {"ride": "b", "leg_s": 100, "osrm_s": 50, "wait_s": 130, "match_s": 20}),
          ("pickup", {"ride": "early", "leg_s": 999, "osrm_s": 999, "wait_s": 999, "match_s": 1}),
          ("fleet", {"drivers": 10, "sim_hours": 2.0, "empty_km": 40.0, "loaded_km": 90.0, "conflicts": 0})]
    for i, (kind, f) in enumerate(ev):
        r.xadd("stream:events", {"type": kind, **f}, id=f"{2000 + i}-0")
    for ride, status in (("a", "completed"), ("b", "assigned"), ("c", "unmatched"), ("early", "completed")):
        r.hset(f"ride:{ride}", mapping={"status": status, "attempts": 1})
    rides = {"a": {"req_sim": 1200.0, "real_s": 600}, "b": {"req_sim": 1300.0, "real_s": 500},
             "c": {"req_sim": 2000.0, "real_s": 400}, "early": {"req_sim": 100.0, "real_s": 300}}
    out = record(r, "run-x", start_id, "9999-0", rides,
                 {"policy": "greedy-distance", "seed": "1", "window_s": "7200"}, tmp_path)

    m = run_metrics(out, warmup_s=900)
    assert m["requests"] == 3 and m["picked_up"] == 2       # the warm-up ride is excluded
    assert m["pickup_mean_s"] == 200 and m["wait_mean_s"] == 245
    assert m["unmatched_pct"] == pytest.approx(100 / 3)
    assert m["empty_km_per_driver_h"] == 2.0                # 40 km / (10 drivers x 2 h)
    assert m["congestion_x"] == 2.0 and m["conflicts"] == 0
    assert 40 < m["decision_p99_ms"] <= 50
    assert json.loads((out / "run.json").read_text())["rides"] == 4


def test_recorder_handles_a_full_run_quickly(r, tmp_path):
    """Regression: row-by-row inserts took >10 min for a 35k-event run. The bulk path must stay fast."""
    import time as _time
    pipe = r.pipeline(transaction=False)
    for i in range(30_000):
        pipe.xadd("stream:events", {"type": "batch", "ms": i % 50, "n": 3}, id=f"{1000 + i}-0")
    pipe.execute()
    t = _time.perf_counter()
    out = record(r, "big", "0-0", "+", {}, {"policy": "x", "window_s": "1"}, tmp_path)
    assert _time.perf_counter() - t < 10
    assert duckdb.connect().execute(f"SELECT count(*) FROM '{(out / 'events.parquet').as_posix()}'").fetchone()[0] == 30_000


def test_cruise_counts_as_empty_and_an_offer_interrupts_it():
    f = Fleet.at([(40.75, -73.98)], seq_base=1)
    assert f.start_cruise("rd0", Leg("cruise", "", _route(600), 0.0, 60.0))
    assert f.drivers["rd0"].mode == IDLE                       # cruising is still idle: matchable
    assert f.accept("rd0", "r1") and f.drivers["rd0"].leg is None   # the offer interrupts the cruise
    assert not f.start_cruise("rd0", Leg("cruise", "", _route(600), 0.0, 60.0))   # busy: no new cruise

    g = Fleet.at([(40.75, -73.98)], seq_base=1)
    g.start_cruise("rd0", Leg("cruise", "", _route(900), 0.0, 60.0))
    [(kind, d, _)] = g.advance(60.0)
    assert kind == "cruise" and d.mode == IDLE and d.empty_m == 900 and d.leg is None


def test_demo_points_are_real_nyc_locations():
    from sim import points
    from core.config import LAT_MAX, LAT_MIN, LNG_MAX, LNG_MIN
    pts = points.load()
    assert len(pts) == 40_000
    assert ((pts[:, 0] >= LAT_MIN) & (pts[:, 0] <= LAT_MAX) & (pts[:, 1] >= LNG_MIN) & (pts[:, 1] <= LNG_MAX)).all()


def test_live_demo_resumes_trips_after_a_restart(r):
    """Regression: a redeploy restarted the demo sim, which forgot every trip in progress; Redis kept those
    drivers 'assigned' with nobody to finish them (4,922 of 5,000 stuck). A restart must resume them."""
    import argparse
    from sim.live import Live
    from sim.agents import ROUTING
    lv = Live(argparse.Namespace(seed=0, drivers=4, id_prefix="d"))
    r.hset("driver:d0", mapping={"lat": 40.75, "lng": -73.98, "status": "assigned", "ride": "r1"})
    r.hset("ride:r1", mapping={"status": "assigned", "driver": "d0", "lat": 40.751, "lng": -73.981,
                               "dlat": 40.77, "dlng": -73.96})
    r.hset("driver:d1", mapping={"lat": 40.70, "lng": -74.00, "status": "assigned", "ride": "r2"})
    r.hset("ride:r2", mapping={"status": "offered", "driver": "d1", "lat": 40.705, "lng": -74.005})
    r.hset("driver:d2", mapping={"lat": 40.72, "lng": -73.99, "status": "available"})
    r.hset("driver:d3", mapping={"lat": 40.73, "lng": -73.95, "status": "assigned", "ride": "gone"})   # stale ride

    todo = sorted(lv.resume(r))
    assert todo[0][:3] == ("d0", "r1", "trip") and todo[0][3] == (40.77, -73.96)      # finish the trip
    assert todo[1][:3] == ("d1", "r2", "pickup") and todo[1][3] == (40.705, -74.005)  # take up the offer
    assert len(todo) == 2
    f = lv.fleet.drivers
    assert (f["d0"].lat, f["d2"].lng) == (40.75, -73.99)                               # last known positions
    assert f["d0"].mode == ROUTING and f["d1"].mode == ROUTING and f["d2"].mode == IDLE and f["d3"].mode == IDLE
