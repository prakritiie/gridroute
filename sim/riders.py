"""Synthetic rider demand for M1 (M2 replaces it with the June 2016 replay).

    python -m sim.riders --rate 5          # ~5 ride requests per second, Poisson arrivals

Pickups and drop-offs are real 2016 taxi locations (sim/data/nyc_points.npy), 0.5-8 km apart, so riders
stand on streets, never in a river.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time

import httpx
import numpy as np

from core.osrm import _hav_m
from sim import points

POINTS = points.load()


def make_requests(rng: np.random.Generator, k: int, pts: np.ndarray | None = None) -> list[dict]:
    """k ride requests between real 2016 taxi locations (sim/data/nyc_points.npy), 0.5-8 km apart."""
    pts = POINTS if pts is None else pts
    out = []
    for _ in range(k):
        a = pts[rng.integers(len(pts))]
        for _try in range(30):
            b = pts[rng.integers(len(pts))]
            if 500 <= _hav_m((a[0], a[1]), (b[0], b[1])) <= 8000:
                break
        out.append({"lat": round(float(a[0]), 6), "lng": round(float(a[1]), 6),
                    "dropoff_lat": round(float(b[0]), 6), "dropoff_lng": round(float(b[1]), 6)})
    return out


async def run(a: argparse.Namespace) -> None:
    rng = np.random.default_rng(a.seed)
    start = time.monotonic()
    sent = errors = 0
    last_report = start
    async with httpx.AsyncClient(base_url=a.gateway, timeout=5) as client:
        print(f"riders: ~{a.rate}/s -> {a.gateway}/rides")
        while not a.duration or time.monotonic() - start < a.duration:
            t0 = time.monotonic()
            reqs = make_requests(rng, int(rng.poisson(a.rate)))
            results = await asyncio.gather(*(client.post("/rides", json=b) for b in reqs), return_exceptions=True)
            for res in results:
                if isinstance(res, Exception) or res.status_code != 202:
                    errors += 1
                else:
                    sent += 1
            if t0 - last_report >= 10:
                print(f"riders: {sent} requested, {errors} errors in the last {t0 - last_report:.0f}s")
                sent = errors = 0
                last_report = t0
            await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rate", type=float, default=5.0, help="mean ride requests per second")
    ap.add_argument("--duration", type=float, default=0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--gateway", default=os.environ.get("GATEWAY_HTTP", "http://localhost:8000"))
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
