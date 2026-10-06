"""Demand model (spec §8.2): requests per res-7 hex in the next 15 minutes. One global LightGBM, Poisson
objective, hex as a categorical; trained Jan-Apr, early-stopped on May, evaluated on June; GPU when available.

    docker compose --profile ml run --rm train python3 -m ml.demand_train
    python -m ml.demand_train                                  # CPU fallback, same code

Baselines it must beat: seasonal naive (same slot last week) and the 4-week average of that slot.
Metrics: WAPE and MAE per (hex, slot), split into busy and quiet hexes.
Writes models/demand/vN/: model.txt, hexes.json, train.json, metrics.json, card.md (+ docs/model_card_demand.md).
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np

from core import config
from core.demand_features import DEMAND_CATEGORICAL, DEMAND_FEATURES, HISTORY, build, four_week_mean, seasonal_naive
from ml.demand_data import EPOCH, SLOT_S
from ml.device import gpu_name, pick_device

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models" / "demand"
DAY = 96


def slot_start(t: int) -> np.datetime64:
    return EPOCH + np.timedelta64(t * SLOT_S, "s")


def slot_of(day: str) -> int:
    return int((np.datetime64(day) - EPOCH) / np.timedelta64(SLOT_S, "s"))


def matrix(H: np.ndarray, t0: int, t1: int, codes: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Stack features and targets for slots [t0, t1). Returns X, y, slot index of each row."""
    X = np.concatenate([build(H, t, slot_start(t), codes) for t in range(t0, t1)])
    y = np.concatenate([H[:, t] for t in range(t0, t1)])
    tt = np.repeat(np.arange(t0, t1), H.shape[0])
    return X, y, tt


def wape(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.abs(y - p).sum() / max(y.sum(), 1e-9))


def evaluate(H: np.ndarray, booster, codes: np.ndarray, t0: int, t1: int) -> dict:
    X, y, _ = matrix(H, t0, t1, codes)
    preds = {
        "Seasonal naive (same slot last week)": np.concatenate([seasonal_naive(H, t) for t in range(t0, t1)]),
        "4-week average of the slot": np.concatenate([four_week_mean(H, t) for t in range(t0, t1)]),
        "Model (LightGBM, Poisson)": booster.predict(X),
    }
    n_hex = H.shape[0]
    volume = H[:, :slot_of("2016-05-01")].sum(axis=1)
    busy_hex = volume >= np.quantile(volume, 0.8)                       # top 20% of hexes by training volume
    busy = np.tile(busy_hex, t1 - t0)
    out = {"slots": int(t1 - t0), "hexes": int(n_hex), "requests": float(y.sum()), "slices": {}}
    for name, m in (("All hexes", np.ones_like(busy)), ("Busy hexes (top 20%)", busy), ("Quiet hexes", ~busy)):
        out["slices"][name] = {k: {"wape": wape(y[m], v[m]), "mae": float(np.abs(y[m] - v[m]).mean())}
                               for k, v in preds.items()}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, default=3000)
    a = ap.parse_args()
    device = pick_device()
    z = np.load(config.DATA_DIR / "features" / "demand_counts.npz")
    H, hexes = z["counts"], list(z["hexes"])
    codes = np.arange(len(hexes), dtype=np.float32)
    t_train0, t_val0, t_test0 = HISTORY, slot_of("2016-05-01"), slot_of("2016-06-01")
    t_end = H.shape[1]

    t0 = time.monotonic()
    X_tr, y_tr, _ = matrix(H, t_train0, t_val0, codes)
    X_va, y_va, _ = matrix(H, t_val0, t_test0, codes)
    print(f"{len(hexes)} hexes; train {len(y_tr):,} rows (29 Jan - 30 Apr), validate {len(y_va):,} (May); "
          f"features in {time.monotonic() - t0:.0f}s", flush=True)
    params = {"objective": "poisson", "metric": "poisson", "device_type": device, "learning_rate": 0.05,
              "num_leaves": 127, "min_data_in_leaf": 50, "feature_fraction": 0.9, "bagging_fraction": 0.8,
              "bagging_freq": 1, "max_bin": 255, "seed": 7, "verbose": -1}
    dtr = lgb.Dataset(X_tr, y_tr, feature_name=DEMAND_FEATURES, categorical_feature=DEMAND_CATEGORICAL)
    dva = lgb.Dataset(X_va, y_va, reference=dtr)
    t0 = time.monotonic()
    booster = lgb.train(params, dtr, num_boost_round=a.rounds, valid_sets=[dva], valid_names=["may"],
                        callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(250)])
    train_s = time.monotonic() - t0
    print(f"trained on {device} in {train_s:.0f}s, best iteration {booster.best_iteration}", flush=True)

    metrics = evaluate(H, booster, codes, t_test0, t_end)
    MODELS.mkdir(parents=True, exist_ok=True)
    n = max([int(p.name[1:]) for p in MODELS.glob("v*") if p.name[1:].isdigit()] + [0]) + 1
    out = MODELS / f"v{n}"
    out.mkdir()
    booster.save_model(str(out / "model.txt"), num_iteration=booster.best_iteration)
    (out / "hexes.json").write_text(json.dumps(hexes))
    imp = dict(zip(DEMAND_FEATURES, booster.feature_importance("gain").tolist()))
    info = {"version": out.name, "device": device, "gpu": gpu_name() if device != "cpu" else None,
            "train_rows": int(len(y_tr)), "validate_rows": int(len(y_va)), "train_seconds": round(train_s, 1),
            "best_iteration": booster.best_iteration, "params": params,
            "importance_gain": dict(sorted(imp.items(), key=lambda kv: -kv[1]))}
    (out / "train.json").write_text(json.dumps(info, indent=2))
    allm = metrics["slices"]["All hexes"]
    model_w = allm["Model (LightGBM, Poisson)"]["wape"]
    metrics["gate_beats_baselines"] = bool(all(model_w < v["wape"] for k, v in allm.items() if not k.startswith("Model")))
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2))

    rows = ["| Slice | " + " | ".join(k.split(" (")[0] for k in allm) + " |", "|---|" + "---|" * len(allm)]
    for sname, sl in metrics["slices"].items():
        rows.append(f"| {sname} | " + " | ".join(f"{v['wape']:.1%} (MAE {v['mae']:.1f})" for v in sl.values()) + " |")
    total = sum(imp.values()) or 1
    card = f"""# Model card: demand {out.name}

_Generated by `python -m ml.demand_train`. Do not edit by hand._

| | |
|---|---|
| **Question** | How many ride requests will this res-7 hex (~5 km²) get in the next 15 minutes? |
| **Model** | One global LightGBM, Poisson objective, {booster.best_iteration} trees; hex as a categorical |
| **Data** | {len(hexes)} hexes (>= 500 training-month pickups; 99.98% of all requests), 15-min counts |
| **Trained on** | {len(y_tr):,} (hex, slot) rows, 29 Jan – 30 Apr 2016; early stopping on May |
| **Hardware** | `{device}`{' on ' + info['gpu'] if info.get('gpu') else ''}, {train_s:.0f} s |
| **Tested on** | June 2016: {metrics['slots']:,} slots x {len(hexes)} hexes, {metrics['requests']:,.0f} requests |
| **Gate** | beats both baselines on WAPE over all hexes: **{'PASS' if metrics['gate_beats_baselines'] else 'FAIL'}** |

## WAPE (lower is better) and MAE per (hex, 15-min slot)

{chr(10).join(rows)}

## What it leans on (gain share)

{chr(10).join(f'- `{k}`: {100 * v / total:.1f}%' for k, v in list(info['importance_gain'].items())[:6])}

## Known limits

- Yellow cabs only: a taxi view of demand, Manhattan-heavy (quiet hexes are mostly outer boroughs).
- No weather, events or transit outages; a forecast for a storm afternoon will be wrong in the way the history is.
- Forecasts one slot ahead (15 min); repositioning acts on that horizon only.
"""
    (out / "card.md").write_text(card, encoding="utf-8")
    (ROOT / "docs" / "model_card_demand.md").write_text(card, encoding="utf-8")
    print(card)


if __name__ == "__main__":
    main()
