"""k6 load test of the three nearby-lookup backends at 1k -> 100k drivers.

    docker compose --profile bench up -d --build postgis bench-api
    python -m bench.k6run                         # -> bench/results/k6.json

For each size: load the same real June positions into every backend, write the query points, then run
k6 (constant arrival rate) against /near/{backend} one backend at a time.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import numpy as np

from bench.backends import BACKENDS
from bench.micro import real_points

ROOT = Path(__file__).resolve().parent.parent
K6_DIR = Path(__file__).parent / "k6"
OUT = Path(__file__).parent / "results" / "k6.json"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", nargs="*", type=int, default=[1000, 10000, 100000])
    ap.add_argument("--rate", type=int, default=500, help="requests per second")
    ap.add_argument("--duration", default="20s")
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()

    (K6_DIR / "out").mkdir(exist_ok=True)
    q = real_points(2000, a.seed + 1000)
    (K6_DIR / "points.json").write_text(json.dumps([[round(float(la), 6), round(float(ln), 6)] for la, ln in q]))
    results = {"rate": a.rate, "duration": a.duration, "sizes": {}}
    for n in a.sizes:
        pts = real_points(n, a.seed)
        drivers = [(f"d{i}", float(la), float(ln)) for i, (la, ln) in enumerate(pts)]
        results["sizes"][str(n)] = {}
        for name, cls in BACKENDS.items():
            b = cls()
            b.reset()
            b.load(drivers)
            time.sleep(1)
            cmd = ["docker", "compose", "--profile", "bench", "run", "--rm", "k6", "run", "--quiet",
                   "-e", f"BACKEND={name}", "-e", f"SIZE={n}", "-e", f"RATE={a.rate}", "-e", f"DURATION={a.duration}",
                   "/scripts/near.js"]
            subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True)
            m = json.loads((K6_DIR / "out" / f"{name}-{n}.json").read_text())["metrics"]
            d = m["http_req_duration"]["values"]
            res = {"p50_ms": d["med"], "p90_ms": d["p(90)"], "p99_ms": d["p(99)"], "max_ms": d["max"],
                   "rps": m["http_reqs"]["values"]["rate"],
                   "failed_pct": 100 * m["http_req_failed"]["values"]["rate"],
                   "dropped": m.get("dropped_iterations", {}).get("values", {}).get("count", 0)}
            results["sizes"][str(n)][name] = res
            print(f"  {n:>7,} drivers  {name:>8}: p50 {res['p50_ms']:.2f} ms  p99 {res['p99_ms']:.2f} ms  "
                  f"{res['rps']:.0f} req/s  failed {res['failed_pct']:.2f}%  dropped {res['dropped']}", flush=True)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
