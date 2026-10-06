"""Synthetic rider demand for M1 (M2 replaces it with the June 2016 replay).

    python -m sim.riders --rate 5          # ~5 ride requests per second, Poisson arrivals

Each request is a real June 2016 taxi trip (sim/data/nyc_trips.npy): its pickup and drop-off, so trip
lengths follow the real distribution (median ~2.1 km) and riders stand on streets, never in a river.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time

import httpx
import redis
import numpy as np

from core import config
from sim import points

TRIPS = points.load_trips()


def make_requests(rng: np.random.Generator, k: int, trips: np.ndarray | None = None) -> list[dict]:
    """k ride requests, each a real 2016 taxi trip (pickup -> drop-off)."""
    trips = TRIPS if trips is None else trips
    return [{"lat": round(float(t[0]), 6), "lng": round(float(t[1]), 6),
             "dropoff_lat": round(float(t[2]), 6), "dropoff_lng": round(float(t[3]), 6)}
            for t in trips[rng.integers(len(trips), size=k)]]


async def run(a: argparse.Namespace) -> None:
    rng = np.random.default_rng(a.seed)
    start = time.monotonic()
    sent = errors = 0
    last_report = start
    async with httpx.AsyncClient(base_url=a.gateway, timeout=5) as client:
        print(f"riders: ~{a.rate}/s -> {a.gateway}/rides")
        paused_by = redis.Redis.from_url(a.redis_url, decode_responses=True)
        while not a.duration or time.monotonic() - start < a.duration:
            t0 = time.monotonic()
            try:
                if paused_by.exists("demo:paused"):                 # a policy-lab replay owns the city
                    await asyncio.sleep(1.0)
                    continue
            except redis.RedisError:
                pass
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
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    ap.add_argument("--gateway", default=os.environ.get("GATEWAY_HTTP", "http://localhost:8000"))
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
