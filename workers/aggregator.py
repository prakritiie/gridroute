"""Aggregator: hex supply/demand snapshot every 500 ms, KPIs every 1 s (spec §10.7, arrow 12). One instance.

    python -m workers.aggregator

  supply   fresh drivers per res-9 cell (pipelined ZCOUNT over active_cells)
  demand   ride requests per res-9 cell in the last 5 min, read straight off stream:rides (entry ids are
           ms timestamps, so windows need no extra state); matcher retries are skipped
  rollup   res 9 -> res 8 / res 7 by H3 parent, for GET /hexes?res= and the dashboard
  history  15-min request counts per res-7 hex -> demand_hist:{res7} (last 2688 slots = 28 days), for the
           forecaster. Slots follow the sim clock (core.clock), so a x10 replay rolls one every 90 s; counts are
           divided by config:demand_scale (a 20% replay reports full-city numbers, the units the model learnt).
           demand_hist:last_slot is the last slot written; a replay's warm start sets it, and a clock jump
           (backwards, or a gap over MAX_GAP slots, e.g. a laptop sleep) restarts the history instead of
           padding it with zeros that never happened.
  audit    a background thread runs tools.audit every 10 s and publishes the live double-booking count

Writes hexes:snapshot + kpis:latest, publishes ch:hexes + ch:kpis.
"""
from __future__ import annotations

import argparse
import json
import signal
import threading
import time
from collections import Counter, deque

import h3
import redis

from core import clock, config, metrics
from tools.audit import audit

RIDES, EVENTS = "stream:rides", "stream:events"
DEMAND_WINDOW_MS = 5 * 60_000
KPI_WINDOW_MS = 60_000
HIST_LEN = 4 * 672                  # 28 days of 15-min slots (core.demand_features.HISTORY)
LAST_SLOT = "demand_hist:last_slot"
MAX_GAP = 4                         # finished slots we may still fill after a pause (1 h)


def _id_ms(entry_id: str) -> int:
    return int(entry_id.split("-", 1)[0])


class Aggregator:
    def __init__(self, r: redis.Redis, now_ms: int | None = None):
        self.r = r
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        # Start reading 5 min back so a restarted aggregator has a full demand window immediately
        self.rides_cursor = f"{now - DEMAND_WINDOW_MS}-0"
        self.events_cursor = f"{now - DEMAND_WINDOW_MS}-0"
        self.requests: deque[tuple[int, str]] = deque()        # (t_ms, res9 cell)
        self.events: deque[tuple[int, str, int]] = deque()     # (t_ms, type, eta_s)
        self.clock = clock.read(r)
        self.slot_counts: dict[int, Counter[str]] = {}         # sim slot -> res7 -> requests
        self.fleet = {"busy": 0, "online": 0, "double_bookings": 0, "violations": 0, "audited_at": None}

    # ---- input ------------------------------------------------------------
    def _read_new(self, stream: str, cursor: str) -> tuple[list, str]:
        entries = []
        while True:
            batch = self.r.xrange(stream, min=f"({cursor}", max="+", count=5000)
            if not batch:
                return entries, cursor
            entries.extend(batch)
            cursor = batch[-1][0]

    def ingest_streams(self, now: int) -> None:
        rides, self.rides_cursor = self._read_new(RIDES, self.rides_cursor)
        for entry_id, f in rides:
            if f.get("retry"):
                continue
            try:
                cell = h3.latlng_to_cell(float(f["lat"]), float(f["lng"]), config.RES_DRIVER)
            except (KeyError, ValueError):
                continue
            t = _id_ms(entry_id)
            self.requests.append((t, cell))
            slot = clock.slot_of(clock.at(self.clock, t))
            self.slot_counts.setdefault(slot, Counter())[h3.cell_to_parent(cell, config.RES_DEMAND)] += 1

        events, self.events_cursor = self._read_new(EVENTS, self.events_cursor)
        for entry_id, f in events:
            self.events.append((_id_ms(entry_id), f.get("type", ""), int(f.get("eta_s", 0) or 0)))

        while self.requests and self.requests[0][0] < now - DEMAND_WINDOW_MS:
            self.requests.popleft()
        while self.events and self.events[0][0] < now - DEMAND_WINDOW_MS:
            self.events.popleft()

    # ---- outputs ----------------------------------------------------------
    def snapshot(self, now: int) -> dict:
        cells = list(self.r.smembers("active_cells"))
        pipe = self.r.pipeline(transaction=False)
        for c in cells:
            pipe.zcount(f"cell:{c}", now - config.MAX_AGE_MS, "+inf")
        supply = {c: n for c, n in zip(cells, pipe.execute()) if n}
        demand = Counter(cell for _, cell in self.requests)

        res: dict[str, list] = {}
        for resolution in (config.RES_DRIVER, config.RES_ETA, config.RES_DEMAND):
            s, d = Counter(), Counter()
            for c, n in supply.items():
                s[c if resolution == config.RES_DRIVER else h3.cell_to_parent(c, resolution)] += n
            for c, n in demand.items():
                d[c if resolution == config.RES_DRIVER else h3.cell_to_parent(c, resolution)] += n
            res[str(resolution)] = [[c, s[c], d[c]] for c in sorted(s.keys() | d.keys())]
        return {"t": now, "window_s": DEMAND_WINDOW_MS // 1000, "res": res}

    def kpis(self, now: int, snap: dict) -> dict:
        recent = [(t, kind, eta) for t, kind, eta in self.events if t >= now - KPI_WINDOW_MS]
        matched_5m = [eta for _, kind, eta in self.events if kind == "match"]
        unmatched_5m = sum(kind == "unmatched" for _, kind, _ in self.events)
        etas = sorted(matched_5m)
        lag = {}
        for stream, group in ((RIDES, "match"), ("stream:pings", "ingest")):
            try:
                info = next((g for g in self.r.xinfo_groups(stream) if g["name"] == group), None)
            except redis.ResponseError:
                info = None
            lag[group] = int((info or {}).get("lag") or 0) + int((info or {}).get("pending") or 0)
            metrics.stream_pending.labels(stream.split(":", 1)[1]).set(lag[group])
        available = sum(n for _, n, _ in snap["res"][str(config.RES_DRIVER)])
        return {
            "t": now,
            "drivers_available": available,
            "drivers_busy": self.fleet["busy"],
            "drivers_online": self.fleet["online"],
            "requests_per_min": sum(t >= now - KPI_WINDOW_MS for t, _ in self.requests),
            "matched_per_min": sum(kind == "match" for _, kind, _ in recent),
            "completed_per_min": sum(kind == "complete" for _, kind, _ in recent),
            "unmatched_per_min": sum(kind == "unmatched" for _, kind, _ in recent),
            "match_rate_5m": round(len(matched_5m) / (len(matched_5m) + unmatched_5m), 4)
            if matched_5m or unmatched_5m else None,
            "eta_mean_s": round(sum(etas) / len(etas)) if etas else None,
            "eta_p90_s": etas[int(len(etas) * 0.9)] if etas else None,
            "queue_rides": lag["match"],
            "queue_pings": lag["ingest"],
            "double_bookings": self.fleet["double_bookings"],
            "invariant_violations": self.fleet["violations"],
            "audited_at": self.fleet["audited_at"],
        }

    def roll_demand_history(self, now: int) -> int:
        """Append every finished slot's counts to each res-7 history. Returns the number of slots written.
        Runs under WATCH on the last slot and the sim clock: if a replay seeds the history or moves the clock
        meanwhile, this pass is dropped and the next one sees the new state (a seed is never overwritten)."""
        with self.r.pipeline(transaction=True) as pipe:
            try:
                pipe.watch(LAST_SLOT, clock.KEY)
                self.clock = clock.read(pipe)
                cur = clock.slot_of(clock.at(self.clock, now))
                raw = pipe.get(LAST_SLOT)
                last = int(raw) if raw is not None else None
                if last is not None and last == cur - 1:
                    return 0
                hist = [k for k in pipe.scan_iter("demand_hist:*", count=1000) if k != LAST_SLOT]
                if last is None or last >= cur or cur - 1 - last > MAX_GAP:
                    # First run, clock went backwards, or a long pause: start a fresh history from here.
                    pipe.multi()
                    if last is not None and hist:
                        pipe.delete(*hist)
                    pipe.set(LAST_SLOT, cur - 1)
                    pipe.execute()
                    if last is not None:
                        print(f"aggregator: demand history restarted (last slot {last}, now {cur})", flush=True)
                    self._drop_counts_before(cur)
                    return 0
                scale = float(pipe.get("config:demand_scale") or 1.0) or 1.0
                known = {k.split(":", 1)[1] for k in hist}
                for slot in range(last + 1, cur):
                    known |= set(self.slot_counts.get(slot, ()))
                pipe.multi()
                for slot in range(last + 1, cur):
                    counts = self.slot_counts.get(slot, Counter())
                    for hex7 in known:
                        pipe.rpush(f"demand_hist:{hex7}", f"{counts.get(hex7, 0) / scale:.6g}")
                for hex7 in known:
                    pipe.ltrim(f"demand_hist:{hex7}", -HIST_LEN, -1)
                pipe.set(LAST_SLOT, cur - 1)
                pipe.execute()
            except redis.WatchError:
                return 0
        self._drop_counts_before(cur)
        return cur - 1 - last

    def _drop_counts_before(self, slot: int) -> None:
        for s_ in [s_ for s_ in self.slot_counts if s_ < slot]:
            del self.slot_counts[s_]

    def run_audit(self) -> None:
        """Fleet counts + invariant audit (slow path, every 10 s in a thread)."""
        now = int(time.time() * 1000)
        busy = online = 0
        keys = list(self.r.scan_iter("driver:*", count=1000, _type="HASH"))
        pipe = self.r.pipeline(transaction=False)
        for k in keys:
            pipe.hmget(k, "status", "ts")
        for status, ts in pipe.execute():
            fresh = now - int(ts or 0) <= config.MAX_AGE_MS
            online += fresh
            busy += status == "assigned"
        violations = audit(self.r)
        c1 = sum(v.startswith("C1") for v in violations)
        self.fleet = {"busy": busy, "online": online, "double_bookings": c1,
                      "violations": len(violations), "audited_at": now}
        metrics.drivers_busy.set(busy)
        metrics.double_bookings.set(c1)
        if violations:
            print("aggregator: AUDIT VIOLATIONS\n  " + "\n  ".join(violations[:10]), flush=True)

    def step(self, now: int | None = None, publish_kpis: bool = True) -> dict:
        now = now if now is not None else int(time.time() * 1000)
        self.clock = clock.read(self.r)
        self.ingest_streams(now)
        snap = self.snapshot(now)
        body = json.dumps(snap, separators=(",", ":"))
        pipe = self.r.pipeline(transaction=False)
        pipe.set("hexes:snapshot", body)
        pipe.publish("ch:hexes", body)
        if publish_kpis:
            k = json.dumps(self.kpis(now, snap), separators=(",", ":"))
            pipe.set("kpis:latest", k)
            pipe.publish("ch:kpis", k)
        pipe.execute()
        self.roll_demand_history(now)
        metrics.drivers_available.set(sum(n for _, n, _ in snap["res"][str(config.RES_DRIVER)]))
        return snap


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    ap.add_argument("--audit-every", type=float, default=10.0)
    a = ap.parse_args()
    metrics.serve_from_env()

    agg = Aggregator(redis.Redis.from_url(a.redis_url, decode_responses=True))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    def audit_loop():
        r2 = redis.Redis.from_url(a.redis_url, decode_responses=True)
        auditor = Aggregator(r2)
        while not stop.is_set():
            try:
                auditor.run_audit()
                agg.fleet = auditor.fleet
            except redis.RedisError as e:
                print(f"aggregator: audit failed: {e}", flush=True)
            stop.wait(a.audit_every)

    threading.Thread(target=audit_loop, daemon=True).start()
    print(f"aggregator: snapshot every {config.AGGREGATOR_INTERVAL_S}s, KPIs every 1s, audit every {a.audit_every}s")
    tick = 0
    while not stop.is_set():
        t0 = time.monotonic()
        agg.step(publish_kpis=tick % 2 == 0)
        tick += 1
        stop.wait(max(0.0, config.AGGREGATOR_INTERVAL_S - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
