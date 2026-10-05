"""Ingest worker: stream:pings -> latest ping per driver -> H3 res-9 cell -> move_driver.lua (spec §6.2, arrow 9).

    python -m workers.ingest                 # run several for more throughput: same group, different consumers

At-least-once: entries are XACKed only after their moves are applied. Duplicates and reordering are
harmless because move_driver.lua ignores any seq <= the stored one. Entries left pending by a crashed
consumer are taken over with XAUTOCLAIM.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import time
from dataclasses import dataclass, field

import h3
import redis

from core import config, metrics
from core.config import LAT_MAX, LAT_MIN, LNG_MAX, LNG_MIN
from core.scripts import Scripts

STREAM = "stream:pings"
GROUP = "ingest"


@dataclass
class Stats:
    read: int = 0
    applied: int = 0
    stale: int = 0          # rejected by the seq check in Lua
    deduped: int = 0        # superseded by a newer ping for the same driver in the same batch
    invalid: int = 0
    lag_ms: list[int] = field(default_factory=list)

    def reset(self) -> "Stats":
        old = Stats(self.read, self.applied, self.stale, self.deduped, self.invalid, self.lag_ms)
        self.__init__()
        return old


class Ingest:
    def __init__(self, r: redis.Redis, consumer: str | None = None, batch: int = 2000, claim_idle_ms: int = 30_000):
        self.r = r
        self.scripts = Scripts(r)
        self.consumer = consumer or f"ingest-{socket.gethostname()}-{os.getpid()}"
        self.batch = batch
        self.claim_idle_ms = claim_idle_ms
        self.stats = Stats()
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            self.r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def step(self, block_ms: int = 1000) -> int:
        """Read one batch of new entries, apply, ack. Returns number of entries handled."""
        try:
            resp = self.r.xreadgroup(GROUP, self.consumer, {STREAM: ">"}, count=self.batch, block=block_ms)
        except redis.ResponseError as e:
            if "NOGROUP" not in str(e):
                raise
            self._ensure_group()                    # stream or group was deleted under us: recreate, carry on
            return 0
        if not resp:
            return 0
        return self._handle(resp[0][1])

    def reclaim(self) -> int:
        """Take over entries another consumer read but never acked (it crashed). Returns entries handled."""
        handled, start = 0, "0-0"
        while True:
            start, entries, _deleted = self.r.xautoclaim(
                STREAM, GROUP, self.consumer, min_idle_time=self.claim_idle_ms, start_id=start, count=self.batch
            )
            handled += self._handle([e for e in entries if e[1]])   # trimmed entries come back empty
            if start == "0-0":
                return handled

    def _handle(self, entries: list[tuple[str, dict[str, str]]]) -> int:
        if not entries:
            return 0
        now = int(time.time() * 1000)
        latest: dict[str, tuple] = {}
        for _id, f in entries:
            try:
                d, lat, lng, seq, t = f["d"], float(f["lat"]), float(f["lng"]), int(f["seq"]), int(f["t"])
            except (KeyError, ValueError):
                self.stats.invalid += 1
                continue
            if not (LAT_MIN <= lat <= LAT_MAX and LNG_MIN <= lng <= LNG_MAX):
                self.stats.invalid += 1
                continue
            prev = latest.get(d)
            if prev is not None:
                self.stats.deduped += 1
                if prev[3] >= seq:
                    continue
            latest[d] = (d, lat, lng, seq, t)

        pings = [(d, h3.latlng_to_cell(lat, lng, config.RES_DRIVER), lat, lng, seq, t)
                 for d, lat, lng, seq, t in latest.values()]
        results = self.scripts.move_drivers(pings)

        applied = sum(results)
        self.stats.read += len(entries)
        self.stats.applied += applied
        self.stats.stale += len(results) - applied
        self.stats.lag_ms.extend(now - p[5] for p in pings)
        metrics.pings_ingested.inc(applied)
        metrics.pings_stale.inc(len(results) - applied)
        for p in pings:
            metrics.ingest_lag.observe(now - p[5])
        self.r.xack(STREAM, GROUP, *[e[0] for e in entries])
        return len(entries)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    ap.add_argument("--consumer", default=None)
    ap.add_argument("--batch", type=int, default=2000)
    a = ap.parse_args()

    metrics.serve_from_env()
    ing = Ingest(redis.Redis.from_url(a.redis_url, decode_responses=True), consumer=a.consumer, batch=a.batch)
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print(f"ingest: consumer {ing.consumer} on {STREAM}/{GROUP}")
    last_claim = last_report = time.monotonic()
    while not stop:
        ing.step(block_ms=500)
        now = time.monotonic()
        if now - last_claim >= 30:
            if n := ing.reclaim():
                print(f"ingest: reclaimed {n} entries from dead consumers")
            last_claim = now
        if now - last_report >= 5:
            s = ing.stats.reset()
            lag = sorted(s.lag_ms)
            p50 = lag[len(lag) // 2] if lag else 0
            p99 = lag[int(len(lag) * 0.99)] if lag else 0
            print(f"ingest: {s.read / (now - last_report):7.0f} pings/s  applied={s.applied} stale={s.stale} "
                  f"deduped={s.deduped} invalid={s.invalid}  lag p50={p50}ms p99={p99}ms")
            last_report = now


if __name__ == "__main__":
    main()
