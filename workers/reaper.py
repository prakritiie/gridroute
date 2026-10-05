"""Reaper: evict drivers that stopped pinging (spec §10.7, arrow 11). One instance.

    python -m workers.reaper

Every 5 s, drivers silent for more than MAX_AGE_MS (30 s) leave their cell, so a killed driver is out
of search within MAX_AGE + interval = 35 s (claim C4). Their driver:{id} hash stays until its 1 h TTL;
if they come back, the next ping re-indexes them.
It also re-queues rides whose 15 s offer lapsed without an accept (lost offer, dead app, ignored):
`offers` is a sorted set scored by lapse time, and expire_offer.lua re-queues each one atomically.
"""
from __future__ import annotations

import argparse
import signal
import time

import redis

from core import config, metrics
from core.scripts import Scripts

BATCH = 500   # cells per Lua call: keeps each script run short so it never stalls Redis


class Reaper:
    def __init__(self, r: redis.Redis, max_age_ms: int = config.MAX_AGE_MS):
        self.r = r
        self.scripts = Scripts(r)
        self.max_age_ms = max_age_ms
        self.last_requeued = 0

    def step(self, now_ms: int | None = None) -> int:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        cutoff = now_ms - self.max_age_ms
        cells = list(self.r.smembers("active_cells"))
        evicted = sum(self.scripts.reap_cells(cells[i:i + BATCH], cutoff) for i in range(0, len(cells), BATCH))
        metrics.drivers_evicted.inc(evicted)
        self.last_requeued = self.expire_offers(now_ms)
        return evicted

    def expire_offers(self, now_ms: int) -> int:
        """Re-queue rides whose offer lease lapsed. Returns how many were re-queued."""
        lapsed = self.r.zrangebyscore("offers", "-inf", now_ms, start=0, num=1000)
        n = sum(self.scripts.expire_offer(ride, now_ms) for ride in lapsed)
        metrics.offers_expired.inc(n)
        return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    ap.add_argument("--interval", type=float, default=config.REAPER_INTERVAL_S)
    a = ap.parse_args()

    metrics.serve_from_env()
    reaper = Reaper(redis.Redis.from_url(a.redis_url, decode_responses=True))
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    print(f"reaper: every {a.interval}s, max age {reaper.max_age_ms} ms")
    while not stop:
        t0 = time.monotonic()
        if n := reaper.step():
            print(f"reaper: evicted {n} silent drivers ({len(reaper.r.smembers('active_cells'))} active cells)")
        if reaper.last_requeued:
            print(f"reaper: re-queued {reaper.last_requeued} rides whose offer lapsed")
        time.sleep(max(0.0, a.interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
