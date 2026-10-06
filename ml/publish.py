"""Publish a model version: point model:{kind}:current at it (spec §7.4 `make publish`, arrow 25).

    python -m ml.publish                       # ETA: the latest evaluated version, only if it passed its gate
    python -m ml.publish --model demand        # demand forecaster, same rule
    python -m ml.publish --version v1 --force

Consumers hot-reload within 30 s (core/eta.py, core/demand.py). Refuses a version whose evaluation failed
its gate unless --force: ETA must beat B2 and B3 overall and on < 3 km; demand must beat both baselines.
"""
from __future__ import annotations

import argparse
import json

import redis

from core import config, demand, eta

KINDS = {
    "eta": (eta.MODELS, eta.POINTER, "gate_beats_B2_B3_overall_and_short"),
    "demand": (demand.MODELS, demand.POINTER, "gate_beats_baselines"),
}


def summary(kind: str, metrics: dict) -> str:
    if kind == "eta":
        m = metrics["slices"]["All June"]["Model (LightGBM)"]
        return f"June MAE {m['mae_s'] / 60:.2f} min, RMSLE {m['rmsle']:.4f}"
    m = metrics["slices"]["All hexes"]["Model (LightGBM, Poisson)"]
    return f"June WAPE {m['wape']:.1%}, MAE {m['mae']:.2f} requests per hex-slot"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", choices=sorted(KINDS), default="eta")
    ap.add_argument("--version")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    a = ap.parse_args()
    root, pointer, gate = KINDS[a.model]
    vdir = root / a.version if a.version else max(
        (p for p in root.glob("v*") if (p / "metrics.json").exists()), key=lambda p: int(p.name[1:]))
    metrics = json.loads((vdir / "metrics.json").read_text(encoding="utf-8"))
    if not metrics.get(gate) and not a.force:
        raise SystemExit(f"{a.model} {vdir.name} failed its evaluation gate; pass --force to publish anyway")
    r = redis.Redis.from_url(a.redis_url, decode_responses=True)
    previous = r.get(pointer)
    r.set(pointer, vdir.name)
    print(f"published {a.model} {vdir.name} (was {previous}): {summary(a.model, metrics)}")


if __name__ == "__main__":
    main()
