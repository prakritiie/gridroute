"""Always-on demo fleet on real streets (replaces M1's straight-line world for the live demo).

    python -m sim.live --drivers 5000
    python -m sim.live --kill-after 60 --kill-frac 0.3        # chaos: silenced drivers stop pinging and accepting

Same machinery as the replay (sim/agents.py): every leg follows an OSRM road route, timed by the June
congestion factor for its area and the current hour of the week. Drivers start at real 2016 taxi locations
(sim/data/nyc_points.npy); idle drivers now and then cruise to a nearby real spot. Without OSRM, legs fall
back to straight lines between real points.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import time
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

from core import config, osrm
from sim import points
from sim.agents import IDLE, Fleet, Leg, straight_route
from sim.congestion import Congestion, hour_of_week
from sim.transport import WebSocketTransport

TICK_S = 0.5
PING_EVERY = 8                     # ticks: one ping per driver every 4 s
CRUISE_P = 0.01                    # per idle driver per tick: a cruise starts every ~50 s on average
CRUISE_M = (500, 3000)
CONGESTION = Path(__file__).parent / "data" / "congestion_june.parquet"


class Live:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.rng = np.random.default_rng(a.seed)
        self.pts = points.load()
        start = self.pts[self.rng.choice(len(self.pts), a.drivers)]
        self.fleet = Fleet.at([(float(p[0]), float(p[1])) for p in start],
                              seq_base=int(time.time() * 1000), prefix=a.id_prefix)
        self.cong = Congestion.load(CONGESTION) if CONGESTION.exists() else None
        self.dropoff: dict[str, tuple[float, float]] = {}
        self.cruise_pending: set[str] = set()      # idle drivers whose cruise route is being fetched
        self.dead: set[str] = set()
        self.pending: set[asyncio.Task] = set()
        self.stop = False

    def sim_now(self) -> float:
        return (time.time() - self.wall0) * self.a.speedup

    def factor(self, lat: float, lng: float) -> float:
        return self.cong.factor(lat, lng, hour_of_week(datetime.now())) if self.cong else 1.85

    def nearby_point(self, lat: float, lng: float) -> tuple[float, float]:
        for _ in range(20):
            p = self.pts[self.rng.integers(len(self.pts))]
            if CRUISE_M[0] <= osrm._hav_m((lat, lng), (float(p[0]), float(p[1]))) <= CRUISE_M[1]:
                return float(p[0]), float(p[1])
        p = self.pts[self.rng.integers(len(self.pts))]
        return float(p[0]), float(p[1])

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.pending.add(t)
        t.add_done_callback(self.pending.discard)

    async def ride_leg(self, driver_id: str, kind: str, ride_id: str, frm, to) -> None:
        route = await osrm.route(self.http, frm, to) or straight_route(frm, to)
        self.fleet.start_leg(driver_id, Leg(kind, ride_id, route, self.sim_now(),
                                            max(1.0, route.duration_s * self.factor(*frm))))

    async def cruise(self, driver_id: str) -> None:
        try:
            d = self.fleet.drivers[driver_id]
            frm = (d.lat, d.lng)
            to = self.nearby_point(*frm)
            route = await osrm.route(self.http, frm, to) or straight_route(frm, to)
            self.fleet.start_cruise(driver_id, Leg("cruise", "", route, self.sim_now(),
                                                   max(1.0, route.duration_s * self.factor(*frm))))
        finally:
            self.cruise_pending.discard(driver_id)

    async def run(self) -> None:
        a = self.a
        self.http = httpx.AsyncClient(base_url=a.osrm, timeout=5)
        self.tx = await WebSocketTransport.connect(a.gateway, list(self.fleet.drivers))
        print(f"live: {len(self.fleet.drivers):,} drivers on real streets via {a.osrm} "
              f"(congestion: {'June factors' if self.cong else 'flat 1.85x'}), x{a.speedup}", flush=True)
        self.wall0 = time.time()
        tick, last, stats = 0, time.time(), {"accepted": 0, "done": 0}
        while not self.stop:
            if a.duration and time.time() - self.wall0 >= a.duration:
                break
            if a.kill_after and not self.dead and time.time() - self.wall0 >= a.kill_after:
                ids = list(self.fleet.drivers)
                self.dead = set(self.rng.choice(ids, int(len(ids) * a.kill_frac), replace=False).tolist())
                print(f"live: killed {len(self.dead):,} drivers (silent: no pings, no accepts)", flush=True)
            now = self.sim_now()

            # offers: accept, then drive to the pickup over the road
            for msg in self.tx.drain():
                d = self.fleet.drivers.get(msg["driver_id"])
                if d is None or d.id in self.dead or not self.fleet.accept(d.id, msg["ride_id"]):
                    continue
                stats["accepted"] += 1
                await self.tx.accept(d.id, msg["ride_id"])
                pick = (float(msg["pickup"][0]), float(msg["pickup"][1]))
                drop = msg.get("dropoff")
                self.dropoff[msg["ride_id"]] = (float(drop[0]), float(drop[1])) if drop else self.nearby_point(*pick)
                self.spawn(self.ride_leg(d.id, "pickup", msg["ride_id"], (d.lat, d.lng), pick))
            for driver_id, ride_id in self.tx.drain_rejected():
                self.fleet.cancel(driver_id, ride_id)

            # movement and arrivals
            for kind, d, leg in self.fleet.advance(now):
                if kind == "pickup":
                    self.spawn(self.ride_leg(d.id, "trip", leg.ride_id, (d.lat, d.lng),
                                             self.dropoff.get(leg.ride_id) or self.nearby_point(d.lat, d.lng)))
                elif kind == "trip":
                    self.dropoff.pop(leg.ride_id, None)
                    stats["done"] += 1
                    await self.tx.trip_done(d.id, leg.ride_id)

            # idle drivers roam now and then, along real streets
            for d in self.fleet.drivers.values():
                if (d.mode == IDLE and d.leg is None and d.id not in self.dead and d.id not in self.cruise_pending
                        and self.rng.random() < CRUISE_P):
                    self.cruise_pending.add(d.id)
                    self.spawn(self.cruise(d.id))

            due = [d for d in self.fleet.due_pings(tick, PING_EVERY) if d.id not in self.dead]
            if due and self.tx.up:
                t_ms = int(time.time() * 1000)
                await self.tx.ws.send(json.dumps({"type": "pings", "pings": [
                    [d.id, round(d.lat, 6), round(d.lng, 6), d.seq, t_ms] for d in due]}))

            if time.time() - last >= 5:
                ds = list(self.fleet.drivers.values())
                busy = sum(d.mode != IDLE for d in ds)
                cruising = sum(d.mode == IDLE and d.leg is not None for d in ds)
                print(f"live: busy {busy:,}  cruising {cruising:,}  +accepted {stats['accepted']}  "
                      f"+done {stats['done']}  conflicts {self.fleet.conflicts}", flush=True)
                stats, last = {"accepted": 0, "done": 0}, time.time()
            tick += 1
            await asyncio.sleep(max(0.0, self.wall0 + tick * TICK_S - time.time()))
        await self.tx.close()
        await self.http.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--drivers", type=int, default=5000)
    ap.add_argument("--speedup", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--duration", type=float, default=0)
    ap.add_argument("--id-prefix", default="d")
    ap.add_argument("--kill-after", type=float, default=0)
    ap.add_argument("--kill-frac", type=float, default=0.3)
    ap.add_argument("--gateway", default=os.environ.get("GATEWAY_WS", "ws://localhost:8000/ws/driver"))
    ap.add_argument("--osrm", default=config.OSRM_URL)
    lv = Live(ap.parse_args())
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(lv, "stop", True))
    asyncio.run(lv.run())


if __name__ == "__main__":
    main()
