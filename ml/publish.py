"""Publish an ETA model version: point model:eta:current at it (spec §7.4 `make publish`, arrow 25).

    python -m ml.publish              # the latest evaluated version, only if it passed its gate
    python -m ml.publish --version v1 --force

Matchers hot-reload the new version within 30 s (core/eta.py). Refuses a version whose evaluation failed
the gate (beats B2 and B3 overall and on < 3 km) unless --force.
"""
from __future__ import annotations

import argparse
import json

import redis

from core import config
from core.eta import MODELS, POINTER


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    a = ap.parse_args()
    vdir = MODELS / a.version if a.version else max(
        (p for p in MODELS.glob("v*") if (p / "metrics.json").exists()), key=lambda p: int(p.name[1:]))
    metrics = json.loads((vdir / "metrics.json").read_text())
    if not metrics.get("gate_beats_B2_B3_overall_and_short") and not a.force:
        raise SystemExit(f"{vdir.name} failed its evaluation gate; pass --force to publish anyway")
    r = redis.Redis.from_url(a.redis_url, decode_responses=True)
    previous = r.get(POINTER)
    r.set(POINTER, vdir.name)
    allm = metrics["slices"]["All June"]["Model (LightGBM)"]
    print(f"published {vdir.name} (was {previous}): June MAE {allm['mae_s'] / 60:.2f} min, RMSLE {allm['rmsle']:.4f}")


if __name__ == "__main__":
    main()
