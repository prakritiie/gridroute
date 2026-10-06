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

from core import chaos, config
from sim import points

TRIPS = points.load_trips()


def make_requests(rng: np.random.Generator, k: int, trips: np.ndarray | None = None) -> list[dict]:
    """k ride requests, each a real 2016 taxi trip (pickup -> drop-off)."""
    trips = TRIPS if trips is None else trips
    return [{"lat": round(float(t[0]), 6), "lng": round(float(t[1]), 6),
             "dropoff_lat": round(float(t[2]), 6), "dropoff_lng": round(float(t[3]), 6)}
            for t in trips[rng.integers(len(trips), size=k)]]


class Burst:
    """Chaos burst: x BURST_FACTOR requests for a while, from real trips that start in the area."""

    def __init__(self):
        self.cursor = "$"
        self.until = 0.0
        self.pool: np.ndarray | None = None

    def poll(self, r: redis.Redis) -> None:
        try:
            if self.cursor == "$":
                last = r.xrevrange(chaos.STREAM, count=1)
                self.cursor = last[0][0] if last else "0-0"
            got = r.xread({chaos.STREAM: self.cursor}, count=20) or []
        except redis.RedisError:
            return
        for _, entries in got:
            for entry_id, fields in entries:
                self.cursor = entry_id
                cmd = chaos.parse(fields)
                if cmd["type"] == "heal":
                    self.until, self.pool = 0.0, None
                elif cmd["type"] == "burst":
                    area = set(cmd["area"]) if cmd.get("area") else None
                    pool = TRIPS if area is None else TRIPS[[chaos.in_area(t[0], t[1], area) for t in TRIPS]]
                    self.pool = pool if len(pool) else TRIPS
                    self.until = time.monotonic() + cmd["duration_s"]
                    print(f"riders: burst x{chaos.BURST_FACTOR} for {cmd['duration_s']:.0f}s "
                          f"from {len(self.pool):,} trips", flush=True)

    def now(self, rate: float) -> tuple[float, np.ndarray | None]:
        if time.monotonic() < self.until:
            return rate * chaos.BURST_FACTOR, self.pool
        return rate, None


async def run(a: argparse.Namespace) -> None:
    rng = np.random.default_rng(a.seed)
    start = time.monotonic()
    sent = errors = 0
    last_report = start
    async with httpx.AsyncClient(base_url=a.gateway, timeout=5) as client:
        print(f"riders: ~{a.rate}/s -> {a.gateway}/rides")
        paused_by = redis.Redis.from_url(a.redis_url, decode_responses=True)
        burst = Burst()
        while not a.duration or time.monotonic() - start < a.duration:
            t0 = time.monotonic()
            try:
                if paused_by.exists("demo:paused"):                 # a policy-lab replay owns the city
                    await asyncio.sleep(1.0)
                    continue
            except redis.RedisError:
                pass
            burst.poll(paused_by)
            rate, pool = burst.now(a.rate)
            reqs = make_requests(rng, int(rng.poisson(rate)), pool)
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
