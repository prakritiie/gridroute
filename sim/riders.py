"""Synthetic rider demand for M1 (M2 replaces it with the June 2016 replay).

    python -m sim.riders --rate 5          # ~5 ride requests per second, Poisson arrivals

Pickups come from the same hotspots as the drivers; dropoffs are 0.5-6 km away in a random direction.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time

import httpx
import numpy as np

from core.config import LAT_MAX, LAT_MIN, LNG_MAX, LNG_MIN
from sim.world import M_PER_DEG_LAT, sample_points


def make_requests(rng: np.random.Generator, k: int) -> list[dict]:
    lat, lng = sample_points(rng, k)
    dist = rng.uniform(500, 6000, k)
    theta = rng.uniform(0, 2 * np.pi, k)
    dlat = np.clip(lat + dist * np.cos(theta) / M_PER_DEG_LAT, LAT_MIN, LAT_MAX)
    dlng = np.clip(lng + dist * np.sin(theta) / (M_PER_DEG_LAT * np.cos(np.radians(lat))), LNG_MIN, LNG_MAX)
    return [{"lat": round(float(a), 6), "lng": round(float(b), 6),
             "dropoff_lat": round(float(c), 6), "dropoff_lng": round(float(d), 6)}
            for a, b, c, d in zip(lat, lng, dlat, dlng)]


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
