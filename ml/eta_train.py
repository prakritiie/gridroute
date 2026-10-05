"""Train the ETA model (spec §8.1): LightGBM on Jan-Apr, early stopping on May, on the GPU when there is one.

    docker compose --profile ml run --rm train python3 -m ml.eta_train      # CUDA (RTX 30xx etc.)
    python -m ml.eta_train                                                   # CPU fallback, same code

Target: log1p(duration_s), so short and long trips weigh fairly (and RMSE on it is RMSLE).
Writes models/eta/vN/: model.txt, vocab.json, baselines.json, train.json. Evaluate with ml.evaluate.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np

from core import config, features
from ml.baselines import Baselines
from ml.device import gpu_name, pick_device

MODELS = Path(__file__).resolve().parent.parent / "models" / "eta"


def load(split: str) -> dict[str, np.ndarray]:
    """Columns as plain arrays. DuckDB returns NULLs as *masked* arrays whose hidden values are 0, which
    numpy math would silently use (an unrouted trip read as 'OSRM says 0 s, 0 m'); they become NaN here."""
    path = (config.DATA_DIR / "features" / f"{split}.parquet").as_posix()
    raw = duckdb.connect().execute(f"SELECT * FROM '{path}'").fetchnumpy()
    out = {}
    for k, v in raw.items():
        if isinstance(v, np.ma.MaskedArray):
            if v.mask.any() and v.dtype.kind != "f":
                raise ValueError(f"{split}.{k} has NULLs in a non-float column")
            v = v.filled(np.nan) if v.dtype.kind == "f" else v.data
        out[k] = np.asarray(v)
    return out


def matrix(d: dict[str, np.ndarray], vocab: features.Vocab) -> np.ndarray:
    return features.build(d["pickup_lat"], d["pickup_lng"], d["dropoff_lat"], d["dropoff_lng"],
                          d["pickup_ts"], d["osrm_s"], d["osrm_m"], vocab)


def next_version() -> Path:
    MODELS.mkdir(parents=True, exist_ok=True)
    n = max([int(p.name[1:]) for p in MODELS.glob("v*") if p.name[1:].isdigit()] + [0]) + 1
    return MODELS / f"v{n}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, default=5000)
    ap.add_argument("--lr", type=float, default=0.05)
    ap.add_argument("--leaves", type=int, default=255)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    device = pick_device()
    tr, va = load("train"), load("validate")
    print(f"train {len(tr['duration_s']):,} rows (Jan-Apr), validate {len(va['duration_s']):,} rows (May)", flush=True)

    t0 = time.monotonic()
    vocab = features.Vocab.fit(np.r_[tr["pickup_lat"], tr["dropoff_lat"]], np.r_[tr["pickup_lng"], tr["dropoff_lng"]])
    X_tr, X_va = matrix(tr, vocab), matrix(va, vocab)
    y_tr, y_va = np.log1p(tr["duration_s"].astype(float)), np.log1p(va["duration_s"].astype(float))
    fi = {f: i for i, f in enumerate(features.FEATURES)}
    base = Baselines.fit(tr["pickup_lat"], tr["pickup_lng"], X_tr[:, fi["how"]], X_tr[:, fi["straight_m"]],
                         tr["osrm_m"], tr["duration_s"].astype(float))
    print(f"features + baselines in {time.monotonic() - t0:.0f}s ({len(vocab.codes):,} res-8 hexes in the vocabulary)", flush=True)

    params = {
        "objective": "regression", "metric": "rmse", "device_type": device,
        "learning_rate": a.lr, "num_leaves": a.leaves, "min_data_in_leaf": 200, "max_bin": 255,
        "feature_fraction": 0.85, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0,
        "cat_smooth": 20, "seed": a.seed, "verbose": -1,
    }
    dtr = lgb.Dataset(X_tr, y_tr, feature_name=features.FEATURES, categorical_feature=features.CATEGORICAL)
    dva = lgb.Dataset(X_va, y_va, reference=dtr)
    t0 = time.monotonic()
    booster = lgb.train(params, dtr, num_boost_round=a.rounds, valid_sets=[dva], valid_names=["may"],
                        callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(250)])
    train_s = time.monotonic() - t0
    rmsle = float(booster.best_score["may"]["rmse"])
    print(f"trained on {device} in {train_s:.0f}s: best iteration {booster.best_iteration}, "
          f"May RMSLE {rmsle:.4f}", flush=True)

    out = next_version()
    out.mkdir(parents=True)
    booster.save_model(str(out / "model.txt"), num_iteration=booster.best_iteration)
    vocab.save(out / "vocab.json")
    base.save(out / "baselines.json")
    imp = dict(zip(features.FEATURES, booster.feature_importance("gain").tolist()))
    (out / "train.json").write_text(json.dumps({
        "version": out.name, "device": device, "gpu": gpu_name() if device != "cpu" else None,
        "train_rows": int(len(y_tr)), "validate_rows": int(len(y_va)), "train_seconds": round(train_s, 1),
        "best_iteration": booster.best_iteration, "validate_rmsle": rmsle, "params": params,
        "features": features.FEATURES, "categorical": features.CATEGORICAL,
        "importance_gain": dict(sorted(imp.items(), key=lambda kv: -kv[1])),
    }, indent=2))
    print(f"saved {out}", flush=True)


if __name__ == "__main__":
    main()
