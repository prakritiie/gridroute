"""Repositioner (spec §8.3, M3, row R4): nudge idle drivers from hexes with more of them than the forecast
needs toward a neighbouring hex that is short.

    python -m workers.repositioner

Every CYCLE_SIM_S of sim time (2 min; 12 s of wall time in a x10 replay):

  need(h)     forecast requests in hex h this slot x config:demand_scale   (from forecast:snapshot)
  idle(h)     fresh available drivers in h (the cell indexes only hold available drivers)
  surplus     idle - need; a hex sends drivers only when its surplus is over SURPLUS_MIN
  target      the k=1 res-7 neighbour with the largest shortage (need - idle > 0)
  how many    at most SHARE of the surplus, never more than the target's shortage, MAX_MOVES per cycle

A driver that was just sent is left alone for COOLDOWN_SIM_S. The nearest idle drivers to the target go;
they drive to the target's busiest res-9 cell of the last 5 minutes (else the hex centre). A move is only a
suggestion: the matcher still sees the driver as available, so it can be offered a ride on the way.

Publishes ch:reposition {"t", "moves": [{driver_id, lat, lng, from, to}]} (the gateway forwards each move to
its driver) and appends type=reposition events to stream:events.
"""
from __future__ import annotations

import argparse
import json
import math
import signal
import threading
import time
from collections import defaultdict

import h3
import redis

from core import clock, config, metrics
from core.search import haversine_m

CHANNEL = "ch:reposition"
CYCLE_SIM_S = 120
COOLDOWN_SIM_S = 600
SURPLUS_MIN = 2.0
SHARE = 0.2
MAX_MOVES = 60


def plan(need: dict[str, float], idle: dict[str, list[tuple[str, float, float]]],
         max_moves: int = MAX_MOVES) -> list[tuple[str, str, str]]:
    """Pure decision: [(driver_id, from_hex, to_hex)]. idle maps res-7 hex -> [(driver, lat, lng)]."""
    gap = {h: need.get(h, 0.0) - len(idle.get(h, ())) for h in set(need) | set(idle)}
    moves: list[tuple[str, str, str]] = []
    sources = sorted((h for h in idle if -gap[h] > SURPLUS_MIN), key=lambda h: gap[h])
    for src in sources:
        surplus = -gap[src]
        send = math.floor(SHARE * surplus)
        if send <= 0:
            continue
        neigh = [n for n in h3.grid_ring(src, 1) if gap.get(n, 0.0) > 0]
        if not neigh:
            continue
        dst = max(neigh, key=lambda n: (gap[n], n))
        k = min(send, math.floor(gap[dst]) or 1, max_moves - len(moves))
        if k <= 0:
            break
        tlat, tlng = h3.cell_to_latlng(dst)
        nearest = sorted(idle[src], key=lambda d: haversine_m(d[1], d[2], tlat, tlng))[:k]
        moves += [(d[0], src, dst) for d in nearest]
        gap[src] += len(nearest)
        gap[dst] -= len(nearest)
        if len(moves) >= max_moves:
            break
    return moves


class Repositioner:
    def __init__(self, r: redis.Redis):
        self.r = r
        self.last_sim: float | None = None
        self.moves_total = 0

    def _idle(self, now_ms: int, cfg: dict | None) -> dict[str, list[tuple[str, float, float]]]:
        cells = list(self.r.smembers("active_cells"))
        pipe = self.r.pipeline(transaction=False)
        for c in cells:
            pipe.zrangebyscore(f"cell:{c}", now_ms - config.MAX_AGE_MS, "+inf")
        ids = [(c, d) for c, ds in zip(cells, pipe.execute()) for d in ds]
        pipe = self.r.pipeline(transaction=False)
        for _, d in ids:
            pipe.hmget(f"driver:{d}", "lat", "lng", "status")
            pipe.exists(f"repos:{d}")
        res = pipe.execute()
        idle: dict[str, list] = defaultdict(list)
        for i, (c, d) in enumerate(ids):
            (lat, lng, status), cooling = res[2 * i], res[2 * i + 1]
            if lat is None or cooling or status == "assigned":
                continue
            idle[h3.cell_to_parent(c, config.RES_DEMAND)].append((d, float(lat), float(lng)))
        return idle

    def _targets(self) -> dict[str, list[tuple[float, float]]]:
        """Per res-7 hex, its res-9 cells with recent requests, busiest first (from hexes:snapshot)."""
        snap = json.loads(self.r.get("hexes:snapshot") or "{}")
        cells = sorted(((c, d) for c, _, d in snap.get("res", {}).get(str(config.RES_DRIVER), []) if d),
                       key=lambda x: -x[1])
        out: dict[str, list] = defaultdict(list)
        for c, _ in cells:
            out[h3.cell_to_parent(c, config.RES_DEMAND)].append(h3.cell_to_latlng(c))
        return out

    def step(self, now_ms: int | None = None, force: bool = False) -> list[dict]:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        if self.r.get("config:reposition") != "1":
            return []
        cfg = clock.read(self.r)
        sim_s = clock.at(cfg, now_ms).timestamp()
        # A replay moves the clock back to 2016: a negative gap means a new timeline, so the cycle is due.
        if not force and self.last_sim is not None and 0 <= sim_s - self.last_sim < CYCLE_SIM_S:
            return []
        self.last_sim = sim_s
        raw = self.r.get("forecast:snapshot")
        if not raw:
            return []
        fc = json.loads(raw)
        scale = float(self.r.get("config:demand_scale") or fc.get("scale") or 1.0)
        need = {h: v * scale for h, v in fc["hexes"].items()}
        decided = plan(need, self._idle(now_ms, cfg))
        if not decided:
            return []
        targets = self._targets()
        speed = float(cfg["speed"]) if cfg else 1.0
        cooldown_ms = max(1000, int(COOLDOWN_SIM_S * 1000 / speed))
        used: dict[str, int] = defaultdict(int)
        moves = []
        for d, src, dst in decided:
            pts = targets.get(dst)
            if pts:
                lat, lng = pts[used[dst] % min(len(pts), 5)]          # spread over the 5 busiest cells
                used[dst] += 1
            else:
                lat, lng = h3.cell_to_latlng(dst)
            moves.append({"driver_id": d, "lat": round(lat, 6), "lng": round(lng, 6), "from": src, "to": dst})
        pipe = self.r.pipeline(transaction=False)
        for m in moves:
            pipe.set(f"repos:{m['driver_id']}", m["to"], px=cooldown_ms)
            pipe.xadd("stream:events", {"type": "reposition", "driver": m["driver_id"], "from": m["from"],
                                        "to": m["to"], "t": now_ms}, maxlen=1_000_000, approximate=True)
        body = json.dumps({"t": now_ms, "moves": moves}, separators=(",", ":"))
        pipe.publish(CHANNEL, body)
        pipe.set("reposition:last", body, ex=3600)          # for playback capture and late dashboards
        pipe.execute()
        self.moves_total += len(moves)
        metrics.repositions.inc(len(moves))
        return moves


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    a = ap.parse_args()
    metrics.serve_from_env()
    rp = Repositioner(redis.Redis.from_url(a.redis_url, decode_responses=True))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print(f"repositioner: every {CYCLE_SIM_S}s of sim time while config:reposition=1", flush=True)
    while not stop.is_set():
        try:
            moves = rp.step()
            if moves:
                print(f"repositioner: sent {len(moves)} drivers ({rp.moves_total:,} total)", flush=True)
        except redis.RedisError as e:
            print(f"repositioner: {e}", flush=True)
        stop.wait(1.0)


if __name__ == "__main__":
    main()
