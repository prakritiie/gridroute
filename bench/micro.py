"""Direct backend benchmark: nearby lookup latency, correctness and write throughput at 1k -> 100k drivers.

    docker compose --profile bench up -d postgis
    python -m bench.micro                       # -> bench/results/micro.json

Driver positions and query points are real June 2016 pickup locations (Manhattan-dense, like the service),
drawn with a fixed seed. Every answer is scored against an exact brute-force top-5 within the radius, so a
backend can't look fast by being wrong.

Measured from one Python client on the same machine as the servers, so absolute numbers include client
and loopback overhead; the comparison between backends is what this is for.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb
import numpy as np

from bench.backends import BACKENDS, NEED, RADIUS_M
from core import config

OUT = Path(__file__).parent / "results" / "micro.json"


def real_points(n: int, seed: int) -> np.ndarray:
    src = (config.DATA_DIR / "clean" / "trips" / "2016-06.parquet").as_posix()
    rows = duckdb.connect().execute(
        f"SELECT pickup_lat, pickup_lng FROM '{src}' USING SAMPLE {n} ROWS (reservoir, {seed})").fetchnumpy()
    return np.column_stack([rows["pickup_lat"], rows["pickup_lng"]])


def exact_top(pts: np.ndarray, ids: np.ndarray, lat: float, lng: float) -> set[str]:
    p1, p2 = np.radians(lat), np.radians(pts[:, 0])
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(pts[:, 1] - lng) / 2) ** 2
    d = 2 * 6_371_000 * np.arcsin(np.sqrt(a))
    inside = np.flatnonzero(d <= RADIUS_M)
    return set(ids[inside[np.argsort(d[inside])[:NEED]]])


def bench_size(n: int, queries: int, backends: list[str], seed: int) -> dict:
    pts = real_points(n, seed)
    ids = np.array([f"d{i}" for i in range(n)])
    drivers = [(d, float(la), float(ln)) for d, (la, ln) in zip(ids, pts)]
    q = [(float(la), float(ln)) for la, ln in real_points(queries, seed + 1000)]   # plain floats for every client
    truth = [exact_top(pts, ids, la, ln) for la, ln in q]
    rng = np.random.default_rng(seed)
    out = {}
    for name in backends:
        b = BACKENDS[name]()
        b.reset()
        t0 = time.perf_counter()
        b.load(drivers)
        load_s = time.perf_counter() - t0

        for la, ln in q[:50]:                                  # warm-up
            b.near(la, ln)
        lat_ms, recall = [], []
        for (la, ln), exact in zip(q, truth):
            t = time.perf_counter()
            got = b.near(la, ln)
            lat_ms.append((time.perf_counter() - t) * 1000)
            if exact:
                recall.append(len(set(got) & exact) / len(exact))

        # writes: 20 batches of 1,000 drivers moving ~100 m (what the ingest path does all day)
        batch, n_batches = min(1000, n), 20
        t0 = time.perf_counter()
        for k in range(n_batches):
            idx = rng.choice(n, batch, replace=False)
            moves = [(ids[i], float(pts[i, 0] + rng.normal(0, 0.0009)), float(pts[i, 1] + rng.normal(0, 0.0009)), 2 + k)
                     for i in idx]
            b.update(moves)
        write_s = time.perf_counter() - t0

        lat_ms = np.array(lat_ms)
        out[name] = {"p50_ms": float(np.percentile(lat_ms, 50)), "p99_ms": float(np.percentile(lat_ms, 99)),
                     "mean_ms": float(lat_ms.mean()), "recall_at_5": float(np.mean(recall)),
                     "writes_per_s": batch * n_batches / write_s, "load_s": load_s}
        r = out[name]
        print(f"  {n:>7,} drivers  {name:>8}: p50 {r['p50_ms']:.3f} ms  p99 {r['p99_ms']:.3f} ms  "
              f"recall@5 {r['recall_at_5']:.4f}  writes {r['writes_per_s']:,.0f}/s", flush=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", nargs="*", type=int, default=[1000, 10000, 100000])
    ap.add_argument("--queries", type=int, default=2000)
    ap.add_argument("--backends", nargs="*", default=list(BACKENDS))
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    results = {"radius_m": RADIUS_M, "need": NEED, "queries": a.queries, "seed": a.seed,
               "sizes": {str(n): bench_size(n, a.queries, a.backends, a.seed) for n in a.sizes}}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
