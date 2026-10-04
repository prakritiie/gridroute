"""Matcher, M1 greedy version (spec §10.6, arrow 10).

    python -m workers.matcher            # run several: same consumer group, rides are split between them

Every 2 s: read new rides from stream:rides; for each ride, in arrival order, ring-search the nearest fresh
drivers and claim them one by one with claim_driver.lua until one sticks. Cost = straight-line distance.
M3 swaps the per-ride greedy loop for OSRM + ETA model + Hungarian assignment over the whole batch.

  claim 1  -> PUBLISH ch:offers, XADD stream:events (match)
  claim 0  -> next candidate; if none left the ride goes back on the stream (attempts += 1, unmatched after 5)
  claim -1 -> ride already handled elsewhere: re-publish its existing assignment
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import time
from dataclasses import dataclass

import redis

from core import config, metrics
from core.scripts import Claim, Scripts, ride_key
from core.search import candidates

RIDES = "stream:rides"
EVENTS = "stream:events"
GROUP = "match"
OFFERS = "ch:offers"

# M1 pickup-time estimate: straight line x detour factor / typical Manhattan speed. Replaced by the ETA model in M3.
DETOUR = 1.3
CITY_SPEED_MPS = 6.0


@dataclass
class Stats:
    rides: int = 0
    matched: int = 0
    conflicts: int = 0      # claim returned 0 because another matcher got the driver first
    requeued: int = 0
    unmatched: int = 0
    already_handled: int = 0


class Matcher:
    def __init__(
        self,
        r: redis.Redis,
        consumer: str | None = None,
        batch: int = config.MATCH_BATCH,
        lease_ms: int = config.TRIP_LEASE_MS,
        claim_idle_ms: int = 30_000,
    ):
        self.r = r
        self.scripts = Scripts(r)
        self.consumer = consumer or f"matcher-{socket.gethostname()}-{os.getpid()}"
        self.batch = batch
        self.lease_ms = lease_ms
        self.claim_idle_ms = claim_idle_ms
        self.stats = Stats()
        self.decision_ms: list[float] = []
        try:
            r.xgroup_create(RIDES, GROUP, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    # ---- stream plumbing -------------------------------------------------
    def step(self, block_ms: int | None = None) -> int:
        """Match one batch of new rides. Returns how many stream entries were handled."""
        resp = self.r.xreadgroup(GROUP, self.consumer, {RIDES: ">"}, count=self.batch, block=block_ms)
        return self._handle_batch(resp[0][1] if resp else [])

    def reclaim(self) -> int:
        """Take over rides a crashed matcher read but never acked."""
        handled, start = 0, "0-0"
        while True:
            start, entries, _ = self.r.xautoclaim(RIDES, GROUP, self.consumer, self.claim_idle_ms, start, count=self.batch)
            handled += self._handle_batch([e for e in entries if e[1]])
            if start == "0-0":
                return handled

    def _handle_batch(self, entries) -> int:
        if not entries:
            return 0
        t0 = time.perf_counter()
        for entry_id, fields in entries:
            self.match_ride(fields.get("ride_id", ""))
        self.r.xack(RIDES, GROUP, *[e[0] for e in entries])
        self.decision_ms.append((time.perf_counter() - t0) * 1000)
        metrics.decision_latency.observe(self.decision_ms[-1])
        return len(entries)

    # ---- one ride ---------------------------------------------------------
    def match_ride(self, ride_id: str) -> str:
        """Returns what happened: matched | requeued | unmatched | handled | missing."""
        ride = self.r.hgetall(ride_key(ride_id))
        if not ride:
            return "missing"                                    # expired or bogus entry
        self.stats.rides += 1
        if ride.get("status") != "queued":
            self.stats.already_handled += 1
            if ride.get("status") == "assigned":
                self._publish_offer(ride_id, ride)              # idempotent re-publish
            return "handled"

        lat, lng = float(ride["lat"]), float(ride["lng"])
        now = int(time.time() * 1000)
        for driver_id, dist_m in candidates(self.r, lat, lng, now=now):
            res = self.scripts.claim_driver(driver_id, ride_id, now_ms=now, lease_ms=self.lease_ms)
            if res is Claim.CLAIMED:
                eta_s = round(dist_m * DETOUR / CITY_SPEED_MPS)
                self.r.hset(ride_key(ride_id), mapping={"eta_pred": eta_s, "dist_m": round(dist_m)})
                ride.update(driver=driver_id, eta_pred=str(eta_s))
                self._publish_offer(ride_id, ride)
                self.r.xadd(EVENTS, {"type": "match", "ride": ride_id, "driver": driver_id, "t": now,
                                     "dist_m": round(dist_m), "eta_s": eta_s,
                                     "attempts": int(ride.get("attempts", 0))},
                            maxlen=1_000_000, approximate=True)
                self.stats.matched += 1
                metrics.rides_matched.inc()
                return "matched"
            if res is Claim.RIDE_HANDLED:
                self.stats.already_handled += 1
                fresh = self.r.hgetall(ride_key(ride_id))
                if fresh.get("status") == "assigned":
                    self._publish_offer(ride_id, fresh)
                return "handled"
            self.stats.conflicts += 1
            metrics.claim_conflicts.inc()

        attempts = self.r.hincrby(ride_key(ride_id), "attempts", 1)
        if attempts >= config.MAX_ATTEMPTS:
            if self.scripts.unmatch_ride(ride_id, now):
                self.r.xadd(EVENTS, {"type": "unmatched", "ride": ride_id, "t": now},
                            maxlen=1_000_000, approximate=True)
                self.stats.unmatched += 1
                metrics.rides_unmatched.inc()
            return "unmatched"
        self.r.xadd(RIDES, {"ride_id": ride_id, "lat": lat, "lng": lng, "t": ride.get("t_req", now), "retry": 1},
                    maxlen=100_000, approximate=True)
        self.stats.requeued += 1
        return "requeued"

    def _publish_offer(self, ride_id: str, ride: dict) -> None:
        msg = {"type": "assignment", "driver_id": ride["driver"], "ride_id": ride_id,
               "pickup": [float(ride["lat"]), float(ride["lng"])], "eta_s": int(float(ride.get("eta_pred", 0)))}
        if "dlat" in ride:
            msg["dropoff"] = [float(ride["dlat"]), float(ride["dlng"])]
        self.r.publish(OFFERS, json.dumps(msg))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    ap.add_argument("--consumer", default=None)
    ap.add_argument("--interval", type=float, default=config.MATCH_INTERVAL_S)
    a = ap.parse_args()

    metrics.serve_from_env()
    m = Matcher(redis.Redis.from_url(a.redis_url, decode_responses=True), consumer=a.consumer)
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    print(f"matcher: consumer {m.consumer}, batch every {a.interval}s, greedy + distance")
    last_claim = last_report = time.monotonic()
    while not stop:
        t0 = time.monotonic()
        while m.step() == m.batch:                          # drain a backlog before sleeping
            pass
        if t0 - last_claim >= 30:
            m.reclaim()
            last_claim = t0
        if t0 - last_report >= 10:
            s, lat = m.stats, sorted(m.decision_ms)
            p99 = lat[int(len(lat) * 0.99)] if lat else 0.0
            print(f"matcher: rides={s.rides} matched={s.matched} requeued={s.requeued} unmatched={s.unmatched} "
                  f"conflicts={s.conflicts} handled={s.already_handled}  batch p99={p99:.1f}ms")
            m.stats, m.decision_ms, last_report = Stats(), [], t0
        time.sleep(max(0.0, a.interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
