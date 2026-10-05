"""Matcher (spec §10.6, arrows 10, 16, 17).

    python -m workers.matcher            # run several: same consumer group, rides are split between them

Every 2 s it reads new rides from stream:rides and matches them under one of four policies (spec §8.4 rows),
chosen per run through Redis `config:policy` (else MATCH_POLICY, default greedy-distance):

  greedy-distance  R0  per ride in arrival order, nearest driver by straight line (the M1 path)
  greedy-eta       R1  per ride in arrival order, lowest predicted pickup time
  batch-distance   R2  Hungarian assignment over the batch, minimising total straight-line distance
  batch-eta        R3  Hungarian assignment over the batch, minimising total predicted pickup time

ETA cost: one OSRM /table call for every candidate driver x every pickup in the batch (150 ms budget), then
the model through core/features.py (the trainer's own code). If OSRM misses the budget the batch falls back
to B3 (hex-hour speeds) and eta_fallback_total counts it: dispatch never waits on the model.
Every match leaves explain:{ride} (candidates, OSRM vs model seconds, cost, choice) for GET /rides/{id}/explain.

  claim 1  -> PUBLISH ch:offers, XADD stream:events (match)
  claim 0  -> next candidate; if none left the ride goes back on the stream (attempts += 1, unmatched at the limit)
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

import numpy as np
import redis
from scipy.optimize import linear_sum_assignment

from core import clock, config, metrics, osrm
from core.eta import EtaRegistry
from core.scripts import Claim, Scripts, ride_key
from core.search import candidates, haversine_m

RIDES = "stream:rides"
EVENTS = "stream:events"
GROUP = "match"
OFFERS = "ch:offers"

# Distance-policy pickup estimate (R0, R2): straight line x detour / typical Manhattan speed
DETOUR = 1.3
CITY_SPEED_MPS = 6.0
POLICIES = ("greedy-distance", "greedy-eta", "batch-distance", "batch-eta")
BATCH_NEED = 8          # candidates per ride when assigning a whole batch (room for Hungarian to trade)
EXPLAIN_TTL_S = 3600
NO_PAIR = 1e9


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
        lease_ms: int | None = None,
        claim_idle_ms: int = 30_000,
        offers: bool = config.OFFERS,
    ):
        self.r = r
        self.scripts = Scripts(r)
        self.consumer = consumer or f"matcher-{socket.gethostname()}-{os.getpid()}"
        self.batch = batch
        # Offers (M2): a 15 s lease the driver must convert with accept_offer.lua, else the reaper re-queues.
        # Without offers (M1) the claim covers the whole trip.
        self.state = "offered" if offers else "assigned"
        self.lease_ms = lease_ms or (config.OFFER_LEASE_MS if offers else config.TRIP_LEASE_MS)
        self.claim_idle_ms = claim_idle_ms
        self.stats = Stats()
        self.decision_ms: list[float] = []
        self.max_attempts = config.MAX_ATTEMPTS
        self.policy = config.MATCH_POLICY
        self.registry = EtaRegistry(r)
        try:
            r.xgroup_create(RIDES, GROUP, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    # ---- stream plumbing -------------------------------------------------
    def step(self, block_ms: int | None = None) -> int:
        """Match one batch of new rides. Returns how many stream entries were handled."""
        # Run-level knobs: a replay sets rider patience (sim time) and the policy under test
        self.max_attempts = int(self.r.get("config:max_attempts") or config.MAX_ATTEMPTS)
        policy = self.r.get("config:policy") or config.MATCH_POLICY
        self.policy = policy if policy in POLICIES else "greedy-distance"
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
        if self.policy == "greedy-distance":
            for _entry_id, fields in entries:
                self.match_ride(fields.get("ride_id", ""))
        else:
            self.match_batch([fields.get("ride_id", "") for _, fields in entries])
        self.r.xack(RIDES, GROUP, *[e[0] for e in entries])
        self.decision_ms.append((time.perf_counter() - t0) * 1000)
        metrics.decision_latency.observe(self.decision_ms[-1])
        # Per-batch latency in the event log, so each replay run reports its own decision p99
        self.r.xadd(EVENTS, {"type": "batch", "ms": round(self.decision_ms[-1], 2), "n": len(entries),
                             "t": int(time.time() * 1000), "policy": self.policy}, maxlen=1_000_000, approximate=True)
        return len(entries)

    # ---- one ride (R0: greedy, straight-line distance) ---------------------
    def match_ride(self, ride_id: str) -> str:
        """Returns what happened: matched | requeued | unmatched | handled | missing."""
        ride = self.r.hgetall(ride_key(ride_id))
        if not ride:
            return "missing"                                    # expired or bogus entry
        self.stats.rides += 1
        if ride.get("status") != "queued":
            self.stats.already_handled += 1
            if ride.get("status") in ("offered", "assigned"):
                self._publish_offer(ride_id, ride)              # idempotent re-publish
            return "handled"
        now = int(time.time() * 1000)
        options = [{"driver": d, "dist_m": round(m), "cost": round(m)}
                   for d, m in candidates(self.r, float(ride["lat"]), float(ride["lng"]), now=now)]
        for opt in options:
            res = self._try(ride_id, ride, opt, options, now, {"cost_unit": "m", "model": None, "fallback": False})
            if res != "conflict":
                return res
        return self._no_driver(ride_id, ride, now)

    # ---- a whole batch (R1-R3) ---------------------------------------------
    def match_batch(self, ride_ids: list[str]) -> None:
        matching, cost_kind = self.policy.split("-")
        now = int(time.time() * 1000)
        pipe = self.r.pipeline(transaction=False)
        for rid in ride_ids:
            pipe.hgetall(ride_key(rid))
        queued: list[tuple[str, dict]] = []
        seen: set[str] = set()
        for rid, ride in zip(ride_ids, pipe.execute()):
            if not ride or rid in seen:
                continue
            seen.add(rid)
            self.stats.rides += 1
            if ride.get("status") != "queued":
                self.stats.already_handled += 1
                if ride.get("status") in ("offered", "assigned"):
                    self._publish_offer(rid, ride)
                continue
            queued.append((rid, ride))
        if not queued:
            return

        need = BATCH_NEED if matching == "batch" else config.CANDIDATES_NEED
        cands = {rid: candidates(self.r, float(ride["lat"]), float(ride["lng"]), need=need, now=now)
                 for rid, ride in queued}
        options, meta = self._score(queued, cands, cost_kind)

        if matching == "batch":
            first = self._assign(queued, options)
            # cheapest pairs claim first; a ride whose pair is lost falls back to its other candidates
            order = sorted(queued, key=lambda q: next(
                (o["cost"] for o in options[q[0]] if o["driver"] == first.get(q[0])), NO_PAIR))
            plan = [(rid, ride, sorted(options[rid], key=lambda o: (o["driver"] != first.get(rid), o["cost"])))
                    for rid, ride in order]
        else:
            plan = [(rid, ride, sorted(options[rid], key=lambda o: o["cost"])) for rid, ride in queued]

        taken: set[str] = set()
        for rid, ride, opts in plan:
            result = None
            for opt in opts:
                if opt["driver"] in taken:
                    continue
                result = self._try(rid, ride, opt, opts, now, meta)
                if result == "matched":
                    taken.add(opt["driver"])
                if result != "conflict":
                    break
            if result in (None, "conflict"):
                self._no_driver(rid, ride, now)

    @staticmethod
    def _assign(queued, options) -> dict[str, str]:
        """Hungarian assignment: ride -> driver minimising the batch's total cost (rides without a pair omitted)."""
        drivers = sorted({o["driver"] for rid, _ in queued for o in options[rid]})
        if not drivers:
            return {}
        col = {d: j for j, d in enumerate(drivers)}
        cost = np.full((len(queued), len(drivers)), NO_PAIR)
        for i, (rid, _) in enumerate(queued):
            for o in options[rid]:
                cost[i, col[o["driver"]]] = o["cost"]
        rows, cols = linear_sum_assignment(cost)
        return {queued[i][0]: drivers[j] for i, j in zip(rows, cols) if cost[i, j] < NO_PAIR}

    def _score(self, queued, cands, cost_kind) -> tuple[dict, dict]:
        """Per ride: [{driver, dist_m, (osrm_s, osrm_m, model_s), cost}] and batch facts for explain."""
        options = {rid: [{"driver": d, "dist_m": round(m), "cost": round(m)} for d, m in cands[rid]]
                   for rid, _ in queued}
        meta = {"cost_unit": "m", "model": None, "fallback": False}
        if cost_kind != "eta":
            return options, meta
        model = self.registry.current()
        drivers = sorted({o["driver"] for opts in options.values() for o in opts})
        if model is None or not drivers:
            return options, {**meta, "fallback": model is None}     # no published model: distance until there is
        pipe = self.r.pipeline(transaction=False)
        for d in drivers:
            pipe.hmget(f"driver:{d}", "lat", "lng")
        pos = {d: (float(p[0]), float(p[1])) for d, p in zip(drivers, pipe.execute()) if p[0] is not None}
        drivers = [d for d in drivers if d in pos]
        row = {d: j for j, d in enumerate(drivers)}
        pickups = [(float(ride["lat"]), float(ride["lng"])) for _, ride in queued]
        pairs = [(i, o) for i, (rid, _) in enumerate(queued) for o in options[rid] if o["driver"] in row]
        if not pairs:
            return options, meta
        when = clock.now(self.r)
        plat = np.array([pos[o["driver"]][0] for _, o in pairs])
        plng = np.array([pos[o["driver"]][1] for _, o in pairs])
        dlat = np.array([pickups[i][0] for i, _ in pairs])
        dlng = np.array([pickups[i][1] for i, _ in pairs])
        table = osrm.table([pos[d] for d in drivers], pickups)
        if table is not None:
            dur, dist = table
            cell = lambda m, i, o: m[row[o["driver"]]][i]  # noqa: E731
            o_s = np.array([np.nan if cell(dur, i, o) is None else cell(dur, i, o) for i, o in pairs], dtype=float)
            o_m = np.array([np.nan if cell(dist, i, o) is None else cell(dist, i, o) for i, o in pairs], dtype=float)
            secs = model.predict(plat, plng, dlat, dlng, np.full(len(pairs), np.datetime64(when, "s")), o_s, o_m)
        else:
            metrics.eta_fallback.inc()
            meta["fallback"] = True
            o_s = o_m = np.full(len(pairs), np.nan)
            straight = np.array([haversine_m(a, b, c, d) for a, b, c, d in zip(plat, plng, dlat, dlng)])
            secs = model.fallback(plat, plng, np.full(len(pairs), when.weekday() * 24 + when.hour), straight)
        for (_, o), s_, os_, om_ in zip(pairs, secs, o_s, o_m):
            o.update(model_s=round(float(s_), 1), cost=round(float(s_), 1),
                     osrm_s=None if np.isnan(os_) else round(float(os_), 1),
                     osrm_m=None if np.isnan(om_) else round(float(om_)))
        for rid, _ in queued:                      # drivers whose position vanished between search and scoring
            options[rid] = [o for o in options[rid] if "model_s" in o]
        return options, {**meta, "cost_unit": "s", "model": model.version, "sim_time": when.isoformat(timespec="seconds")}

    # ---- shared tails ------------------------------------------------------
    def _try(self, ride_id: str, ride: dict, opt: dict, options: list[dict], now: int, meta: dict) -> str:
        """Claim one candidate. Returns matched | handled | conflict."""
        driver_id = opt["driver"]
        res = self.scripts.claim_driver(driver_id, ride_id, now_ms=now, lease_ms=self.lease_ms, state=self.state)
        if res is Claim.CLAIMED:
            eta_s = round(opt["model_s"]) if "model_s" in opt else round(opt["dist_m"] * DETOUR / CITY_SPEED_MPS)
            self.r.hset(ride_key(ride_id), mapping={
                "eta_pred": eta_s, "dist_m": opt["dist_m"], "policy": self.policy,
                "eta_model": meta.get("model") or "", "eta_fallback": int(bool(meta.get("fallback")))})
            ride.update(driver=driver_id, eta_pred=str(eta_s))
            self._publish_offer(ride_id, ride)
            self.r.xadd(EVENTS, {"type": "match", "ride": ride_id, "driver": driver_id, "t": now,
                                 "dist_m": opt["dist_m"], "eta_s": eta_s, "attempts": int(ride.get("attempts", 0)),
                                 "policy": self.policy}, maxlen=1_000_000, approximate=True)
            self.r.set(f"explain:{ride_id}", json.dumps({
                "ride": ride_id, "policy": self.policy, "t": now, "chosen": driver_id, **meta,
                "pickup": [float(ride["lat"]), float(ride["lng"])], "candidates": options}), ex=EXPLAIN_TTL_S)
            self.stats.matched += 1
            metrics.rides_matched.inc()
            return "matched"
        if res is Claim.RIDE_HANDLED:
            self.stats.already_handled += 1
            fresh = self.r.hgetall(ride_key(ride_id))
            if fresh.get("status") in ("offered", "assigned"):
                self._publish_offer(ride_id, fresh)
            return "handled"
        self.stats.conflicts += 1
        metrics.claim_conflicts.inc()
        return "conflict"

    def _no_driver(self, ride_id: str, ride: dict, now: int) -> str:
        attempts = self.r.hincrby(ride_key(ride_id), "attempts", 1)
        if attempts >= self.max_attempts:
            if self.scripts.unmatch_ride(ride_id, now):
                self.r.xadd(EVENTS, {"type": "unmatched", "ride": ride_id, "t": now},
                            maxlen=1_000_000, approximate=True)
                self.stats.unmatched += 1
                metrics.rides_unmatched.inc()
            return "unmatched"
        self.r.xadd(RIDES, {"ride_id": ride_id, "lat": ride["lat"], "lng": ride["lng"],
                            "t": ride.get("t_req", now), "retry": 1}, maxlen=100_000, approximate=True)
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
    print(f"matcher: consumer {m.consumer}, batch every {a.interval}s, default policy {config.MATCH_POLICY}")
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
            print(f"matcher: [{m.policy}] rides={s.rides} matched={s.matched} requeued={s.requeued} "
                  f"unmatched={s.unmatched} conflicts={s.conflicts} handled={s.already_handled}  batch p99={p99:.1f}ms")
            m.stats, m.decision_ms, last_report = Stats(), [], t0
        time.sleep(max(0.0, a.interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
