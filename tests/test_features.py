"""Shared ETA features and baselines (spec §14: test_feature_parity, test_no_leakage, test_model_beats_baselines)."""
import json
from pathlib import Path

import h3
import numpy as np
import pytest

from core import config, features
from core.features import FEATURES, FORBIDDEN, INPUTS, Vocab, build, build_one
from ml.baselines import Baselines

ROOT = Path(__file__).resolve().parent.parent


def _rows(n=1000, seed=3):
    rng = np.random.default_rng(seed)
    t0 = np.datetime64("2016-06-01T00:00:00")
    return {
        "pickup_lat": 40.70 + rng.random(n) * 0.12, "pickup_lng": -74.02 + rng.random(n) * 0.12,
        "dropoff_lat": 40.70 + rng.random(n) * 0.12, "dropoff_lng": -74.02 + rng.random(n) * 0.12,
        "pickup_ts": t0 + rng.integers(0, 30 * 86400, n).astype("timedelta64[s]"),
        "osrm_s": np.where(rng.random(n) < 0.02, np.nan, rng.uniform(60, 3000, n)),   # some unrouted legs
        "osrm_m": rng.uniform(300, 20000, n),
    }


def test_feature_parity():
    """Training (batch) and serving (one request at a time) produce identical rows, NaNs included."""
    d = _rows()
    vocab = Vocab.fit(d["pickup_lat"], d["pickup_lng"], top=200, min_count=1)
    batch = build(*(d[k] for k in INPUTS), vocab)
    online = np.stack([build_one(*(d[k][i] for k in INPUTS), vocab) for i in range(len(d["osrm_s"]))])
    assert batch.shape == (1000, len(FEATURES)) and batch.dtype == np.float32
    np.testing.assert_array_equal(batch, online)


def test_no_leakage():
    for name in FEATURES + INPUTS:
        assert not any(bad in name.lower() for bad in FORBIDDEN), f"{name} leaks the answer or isn't known at dispatch"


def test_known_values():
    vocab = Vocab({})
    fi = {f: i for i, f in enumerate(FEATURES)}
    # Mon 18 Jan 2016 (MLK day) 08:30, JFK -> Midtown
    row = build_one(40.6413, -73.7781, 40.7549, -73.9840, "2016-01-18T08:30:00", 1800.0, 27900.0, vocab)
    assert row[fi["how"]] == 8 and row[fi["hour"]] == 8
    assert row[fi["holiday"]] == 1 and row[fi["pickup_airport"]] == 1 and row[fi["dropoff_airport"]] == 0
    assert 20_000 < row[fi["straight_m"]] < 23_000 and row[fi["detour"]] > 1
    assert np.isnan(row[fi["pickup_r8"]])                    # not in the (empty) vocabulary -> missing
    # Sun 5 Jun 2016 23:00, an ordinary day
    row = build_one(40.75, -73.98, 40.76, -73.97, "2016-06-05T23:00:00", 300.0, 1500.0, vocab)
    assert row[fi["how"]] == 6 * 24 + 23 and row[fi["holiday"]] == 0


def test_vocab_roundtrip(tmp_path):
    d = _rows(300)
    v = Vocab.fit(d["pickup_lat"], d["pickup_lng"], top=50, min_count=1)
    v.save(tmp_path / "v.json")
    w = Vocab.load(tmp_path / "v.json")
    np.testing.assert_array_equal(v.encode(d["pickup_lat"], d["pickup_lng"]), w.encode(d["pickup_lat"], d["pickup_lng"]))


def test_b3_falls_back_from_hex_to_area_to_hour_to_city(tmp_path):
    lat, lng = 40.7549, -73.9840
    r8 = h3.latlng_to_cell(lat, lng, config.RES_ETA)
    n = 40
    b = Baselines.fit(np.full(n, lat), np.full(n, lng), np.full(n, 8), np.full(n, 1000.0),
                      np.full(n, 1200.0), np.full(n, 200.0))           # 6 m/s everywhere it has data
    assert b.b3["res8"][(r8, 8)] == 6.0 and b.city_speed == 5.0
    b.save(tmp_path / "b.json")
    b = Baselines.load(tmp_path / "b.json")
    assert b.b3_speed(lat, lng, 8) == 6.0                       # exact hex + hour
    assert b.b3_speed(40.60, -73.80, 8) == 6.0                  # unknown hex: hour-of-week fallback
    assert b.b3_speed(40.60, -73.80, 99) == 6.0                 # unknown hour: city-wide
    np.testing.assert_allclose(b.b2(np.array([123.0])), [123.0])


def _latest_model():
    vs = sorted((ROOT / "models" / "eta").glob("v*/metrics.json"), key=lambda p: int(p.parent.name[1:]))
    return vs[-1] if vs else None


@pytest.mark.skipif(_latest_model() is None, reason="no evaluated ETA model (run ml.eta_train + ml.evaluate)")
def test_model_beats_baselines():
    m = json.loads(_latest_model().read_text())
    for s in ("All June", "< 3 km (pickup-like)"):
        sl = m["slices"][s]
        assert sl["Model (LightGBM)"]["mae_s"] < sl["B2 raw OSRM"]["mae_s"]
        assert sl["Model (LightGBM)"]["mae_s"] < sl["B3 OSRM dist / hex-hour speed"]["mae_s"]


def test_loader_turns_nulls_into_nan(tmp_path, monkeypatch):
    """Regression: DuckDB NULLs arrive as masked arrays hiding 0s; 50 unrouted trips once trained as 'OSRM: 0 s'."""
    import duckdb
    from ml import eta_train
    (tmp_path / "features").mkdir()
    duckdb.connect().execute(f"""COPY (SELECT * FROM (VALUES (1.0, 10), (NULL, 20)) t(osrm_s, duration_s))
                                 TO '{(tmp_path / 'features' / 'x.parquet').as_posix()}' (FORMAT parquet)""")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    d = eta_train.load("x")
    assert not isinstance(d["osrm_s"], np.ma.MaskedArray)
    assert d["osrm_s"][0] == 1.0 and np.isnan(d["osrm_s"][1])
