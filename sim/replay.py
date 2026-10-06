"""Replay a window of real June 2016 demand through the live system and record the run (spec §6.3, §8.4).

    python -m sim.replay --day 2016-06-07 --start 07:00 --hours 2 --sample 0.2 --drivers 2000 --speed 10 --seed 1
    python -m sim.replay --policy batch-eta --reposition ...        # row R4 (needs forecaster + repositioner)

Needs the stack (gateway, ingest, matcher, reaper, aggregator) and OSRM; the live demo sim/riders must be
stopped. --reset clears the simulated Redis state first.

  demand   real June trips in the window, a seeded `--sample` fraction, requested at their real pickup time
  fleet    `--drivers` cabs placed where real trips dropped off in the hour before the window
  pickup   drive over the OSRM route; true time = OSRM time x June congestion (pickup hex, hour of week)
  trip     the OSRM route, driven in the trip's real recorded duration
  clock    one sim clock for everything: sim = start + wall elapsed x speed
  demand   history warm start: the 28 days of real counts before the window are written to demand_hist:*
           and config:demand_scale = --sample, so the forecaster has history from the first slot
  repos    with --reposition, idle drivers drive (empty) toward the target the repositioner sends

After the window plus a drain period, the recorder writes $GRIDROUTE_DATA_DIR/runs/{run_id}/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import httpx
import redis

from core import clock, config, demand, osrm
from sim.agents import IDLE, Fleet, Leg, straight_route
from sim.congestion import Congestion, hour_of_week
from sim.playback import PlaybackRecorder
from sim.transport import WebSocketTransport
from tools.reset import reset_sim_state
from workers.recorder import record

TICK_S = 0.5                 # wall seconds per tick
PING_EVERY = 8               # ticks -> one ping per driver every 4 wall seconds


def load_window(data_dir: Path, day: str, start: str, hours: float, sample: float, drivers: int, seed: int):
    """Real requests in the window (seeded sample) and real drop-off points just before it for the fleet."""
    t0 = datetime.fromisoformat(f"{day}T{start}")
    t1 = t0 + timedelta(hours=hours)
    src = (data_dir / "clean" / "trips" / f"{day[:7]}.parquet").as_posix()
    con = duckdb.connect()
    con.execute(f"SELECT setseed({(seed % 1000) / 1000})")
    trips = con.execute(f"""
        SELECT pickup_ts, pickup_lat, pickup_lng, dropoff_lat, dropoff_lng, duration_s
        FROM '{src}' WHERE pickup_ts >= '{t0}' AND pickup_ts < '{t1}' AND random() < {sample}
        ORDER BY pickup_ts""").fetchall()
    fleet = con.execute(f"""
        SELECT dropoff_lat, dropoff_lng FROM '{src}'
        WHERE dropoff_ts >= '{t0 - timedelta(hours=1)}' AND dropoff_ts < '{t0}'
        ORDER BY hash(pickup_ts, dropoff_lat, {seed}) LIMIT {drivers}""").fetchall()
    return t0, t1, trips, fleet


class Replay:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.r = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
        self.cong = Congestion.load()
        self.t0, self.t1, self.trips, positions = load_window(
            config.DATA_DIR, a.day, a.start, a.hours, a.sample, a.drivers, a.seed)
        self.fleet = Fleet.at(positions, seq_base=int(time.time() * 1000))
        self.rides: dict[str, dict] = {}            # ride_id -> request facts (sim times, real duration)
        self.label = f"{a.policy}-repos" if a.reposition else a.policy
        self.run_id = f"{a.day}-{a.start.replace(':', '')}-{self.label}-s{a.seed}-{int(time.time())}"
        self.pending: set[asyncio.Task] = set()
        self.stop = False
        self.cancelled = False
        self.playback: PlaybackRecorder | None = None

    # ---- clock ---------------------------------------------------------------
    def sim_now(self) -> float:
        """Sim seconds since the window start."""
        return (time.time() - self.wall0) * self.a.speed

    def sim_dt(self, sim_s: float) -> datetime:
        return self.t0 + timedelta(seconds=sim_s)

    # ---- legs ------------------------------------------------------------------
    async def _route_leg(self, driver_id: str, kind: str, ride_id: str, frm, to, real_s: float | None) -> None:
        route = await osrm.route(self.http, frm, to) or straight_route(frm, to)
        start = self.sim_now()
        if kind == "pickup":
            how = hour_of_week(self.sim_dt(start))
            duration = route.duration_s * self.cong.factor(frm[0], frm[1], how)
        else:
            duration = real_s if real_s else route.duration_s * self.cong.factor(frm[0], frm[1],
                                                                                 hour_of_week(self.sim_dt(start)))
        self.fleet.start_leg(driver_id, Leg(kind, ride_id, route, start, max(duration, 1.0)))

    async def _reposition(self, driver_id: str, frm, to) -> None:
        route = await osrm.route(self.http, frm, to) or straight_route(frm, to)
        start = self.sim_now()
        duration = route.duration_s * self.cong.factor(frm[0], frm[1], hour_of_week(self.sim_dt(start)))
        d = self.fleet.drivers[driver_id]
        if (d.lat, d.lng) != frm:                 # moved meanwhile (an offer came and went): stay put
            return
        self.fleet.reposition(driver_id, Leg("reposition", "", route, start, max(duration, 1.0)))

    def warm_start(self) -> None:
        """Seed the demand history with the real counts of the 28 days before the window."""
        counts, hexes = demand.load_counts()
        first = clock.slot_of(self.t0)
        demand.seed_history(self.r, counts, hexes, data_end=first, live_last=first - 1)
        self.r.set("config:demand_scale", self.a.sample)
        self.r.delete("forecast:snapshot")

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.pending.add(t)
        t.add_done_callback(self.pending.discard)

    async def _emit(self, **event) -> None:
        if self.tx.up:
            await self.tx.ws.send(json.dumps({"type": "event", "event": {"run": self.run_id, **event}}))

    # ---- riders --------------------------------------------------------------
    async def _request(self, trip) -> None:
        ts, plat, plng, dlat, dlng, dur = trip
        body = {"lat": plat, "lng": plng, "dropoff_lat": dlat, "dropoff_lng": dlng}
        try:
            resp = await self.http_gw.post("/rides", json=body)
        except httpx.HTTPError:
            return
        if resp.status_code == 202:
            ride_id = resp.json()["ride_id"]
            self.rides[ride_id] = {"req_sim": (ts - self.t0).total_seconds(), "pickup": (plat, plng),
                                   "dropoff": (dlat, dlng), "real_s": dur, "posted_sim": self.sim_now()}

    # ---- main loop -------------------------------------------------------------
    async def run(self) -> str:
        a = self.a
        if a.reset:
            reset_sim_state(self.r)          # keeps model pointers and consumer groups (not FLUSHDB)
        elif self.r.scard("active_cells"):
            raise SystemExit("Redis already has live drivers: stop the demo sim/riders and pass --reset")
        span = (self.t1 - self.t0).total_seconds()
        drain = a.drain_min * 60
        cfg = {k: str(v) for k, v in vars(a).items()} | {
            "requests": str(len(self.trips)), "window_s": str(span), "drain_s": str(drain)}
        # Rider patience is sim time: retries happen every MATCH_INTERVAL_S of wall time = that x speed sim time
        attempts = max(1, math.ceil(a.patience_min * 60 / (a.speed * config.MATCH_INTERVAL_S)))
        self.r.set("config:max_attempts", attempts)
        self.r.set("config:policy", a.policy)                 # the matcher switches policy for this run
        self.r.set("config:reposition", int(a.reposition))
        cfg["max_attempts"] = str(attempts)
        cfg["policy"] = self.label                             # the report's row key (R4 = batch-eta-repos)
        self.r.hset(f"run:{self.run_id}", mapping=cfg | {"status": "running"})
        self.r.set("run:current", self.run_id)
        busy_est = len(self.trips) / (span / 3600) * (19 / 60)
        print(f"replay {self.run_id}: {len(self.trips):,} requests over {span / 3600:.1f} h sim, "
              f"{len(self.fleet.drivers):,} drivers (~{busy_est:,.0f} busy at steady state), x{a.speed}", flush=True)

        if getattr(a, "playback", False):
            self.playback = PlaybackRecorder(self.r, {
                "run_id": self.run_id, "policy": self.label, "day": a.day, "start": a.start, "hours": a.hours,
                "sample": a.sample, "drivers": a.drivers, "speed": a.speed, "seed": a.seed,
                "window_s": span, "requests": len(self.trips)})
        start_id = f"{int(time.time() * 1000)}-0"
        self.http = httpx.AsyncClient(base_url=config.OSRM_URL, timeout=10)
        self.http_gw = httpx.AsyncClient(base_url=a.gateway_http, timeout=10)
        self.tx = await WebSocketTransport.connect(a.gateway_ws, list(self.fleet.drivers))
        self.wall0 = time.time()
        clock.set_sim_clock(self.r, self.t0, a.speed, int(self.wall0 * 1000))   # the model's "now" is June 2016
        try:
            self.warm_start()                    # after the clock: the aggregator must already see June 2016
        except FileNotFoundError:
            print("replay: no demand counts (python -m ml.demand_data); forecaster starts cold", flush=True)
        next_req, tick, last_report = 0, 0, 0.0

        while not self.stop:
            now = self.sim_now()
            if now >= span + drain:
                break
            # riders: everything due up to now (only inside the window)
            batch = []
            while next_req < len(self.trips) and (self.trips[next_req][0] - self.t0).total_seconds() <= now:
                batch.append(self.trips[next_req])
                next_req += 1
            if batch:
                await asyncio.gather(*(self._request(t) for t in batch))

            # offers -> routing to the pickup
            if self.playback and now <= span:
                self.playback.tick(now)
            for msg in self.tx.drain():
                if self.playback and msg.get("type") == "assignment" and now <= span:
                    self.playback.match(now, msg)
                if msg.get("type") == "reposition":
                    d = self.fleet.drivers.get(msg["driver_id"])
                    if d and d.mode == IDLE:
                        self._spawn(self._reposition(d.id, (d.lat, d.lng), tuple(msg["target"])))
                    continue
                ride = self.rides.get(msg["ride_id"])
                d = self.fleet.drivers.get(msg["driver_id"])
                if ride and d and self.fleet.accept(d.id, msg["ride_id"]):
                    ride["assigned_sim"] = now
                    await self.tx.accept(d.id, msg["ride_id"])
                    self._spawn(self._route_leg(d.id, "pickup", msg["ride_id"], (d.lat, d.lng), ride["pickup"], None))

            for driver_id, ride_id in self.tx.drain_rejected():
                self.fleet.cancel(driver_id, ride_id)

            # movement, arrivals
            for kind, d, leg in self.fleet.advance(now):
                ride = self.rides.get(leg.ride_id, {})
                if kind in ("cruise", "reposition"):
                    continue
                if kind == "pickup":
                    await self._emit(type="pickup", ride=leg.ride_id, driver=d.id, sim_t=round(now, 1),
                                     leg_s=round(leg.duration_s, 1), leg_m=round(leg.route.distance_m),
                                     osrm_s=round(leg.route.duration_s, 1),
                                     wait_s=round(now - ride.get("req_sim", now), 1),
                                     match_s=round(ride.get("assigned_sim", now) - ride.get("req_sim", now), 1))
                    self._spawn(self._route_leg(d.id, "trip", leg.ride_id, ride.get("pickup", (d.lat, d.lng)),
                                                ride.get("dropoff", (d.lat, d.lng)), ride.get("real_s")))
                else:
                    await self.tx.trip_done(d.id, leg.ride_id)
                    await self._emit(type="dropoff", ride=leg.ride_id, driver=d.id, sim_t=round(now, 1),
                                     trip_s=round(leg.duration_s, 1), trip_m=round(leg.route.distance_m))

            # pings
            due = self.fleet.due_pings(tick, PING_EVERY)
            if due and self.tx.up:
                t_ms = int(time.time() * 1000)
                await self.tx.ws.send(json.dumps({"type": "pings", "pings": [
                    [d.id, round(d.lat, 6), round(d.lng, 6), d.seq, t_ms] for d in due]}))

            if tick % 4 == 0:                                   # progress for the policy lab, every 2 s
                self.r.hset(f"run:{self.run_id}", mapping={
                    "sim_time": self.sim_dt(now).isoformat(timespec="seconds"),
                    "progress": round(min(1.0, now / (span + drain)), 4),
                    "requested": len(self.rides)})
                if self.r.exists(f"run:{self.run_id}:cancel"):
                    self.cancelled = self.stop = True
            if time.time() - last_report >= 10:
                last_report = time.time()
                busy = sum(d.mode != IDLE for d in self.fleet.drivers.values())
                print(f"  sim {self.sim_dt(now):%H:%M}  requested {len(self.rides):,}/{len(self.trips):,}  "
                      f"busy {busy:,}  trips done {sum(d.trips for d in self.fleet.drivers.values()):,}  "
                      f"conflicts {self.fleet.conflicts}", flush=True)
            tick += 1
            await asyncio.sleep(max(0.0, self.wall0 + tick * TICK_S - time.time()))

        sim_hours = (span + drain) / 3600
        await self._emit(type="fleet", drivers=len(self.fleet.drivers), sim_hours=round(sim_hours, 4),
                         empty_km=round(sum(d.empty_m for d in self.fleet.drivers.values()) / 1000, 2),
                         loaded_km=round(sum(d.loaded_m for d in self.fleet.drivers.values()) / 1000, 2),
                         repos_km=round(sum(d.repos_m for d in self.fleet.drivers.values()) / 1000, 2),
                         repositions=sum(d.repositions for d in self.fleet.drivers.values()),
                         conflicts=self.fleet.conflicts)
        await asyncio.sleep(1.0)                     # let the gateway append the last events
        end_id = f"{int(time.time() * 1000)}-0"
        await self.tx.close()
        await self.http.aclose()
        await self.http_gw.aclose()
        self.r.hset(f"run:{self.run_id}", mapping={"status": "cancelled" if self.cancelled else "done"})
        self.r.delete("config:max_attempts", "config:policy", "config:reposition", "config:demand_scale", clock.KEY)
        out = record(self.r, self.run_id, start_id, end_id, self.rides, cfg,
                     Path(a.runs_dir) if getattr(a, "runs_dir", None) else config.DATA_DIR / "runs")
        print(f"recorded {out}", flush=True)
        if self.playback:
            print(f"playback {self.playback.write(out / 'playback.json')} "
                  f"({len(self.playback.frames)} frames, {len(self.playback.matches):,} matches)", flush=True)
        return self.run_id


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", default="2016-06-07")
    ap.add_argument("--start", default="07:00")
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--sample", type=float, default=0.2, help="fraction of real requests to replay")
    ap.add_argument("--drivers", type=int, default=1600, help="same fleet for every row; mild scarcity at the peak")
    ap.add_argument("--speed", type=float, default=10.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--policy", default="greedy-distance",
                    choices=["greedy-distance", "greedy-eta", "batch-distance", "batch-eta"],
                    help="set on the running matchers for this run (spec §8.4 rows R0-R3)")
    ap.add_argument("--reposition", action="store_true",
                    help="turn the repositioner on for this run (row R4 with --policy batch-eta)")
    ap.add_argument("--playback", action="store_true",
                    help="also capture frames for the static demo site (sim/playback.py)")
    ap.add_argument("--patience-min", type=float, default=5, help="sim minutes a rider waits for a driver")
    ap.add_argument("--drain-min", type=float, default=20, help="sim minutes after the window to finish trips")
    ap.add_argument("--gateway-http", default="http://localhost:8000")
    ap.add_argument("--gateway-ws", default="ws://localhost:8000/ws/driver")
    ap.add_argument("--reset", action="store_true", help="clear simulated state first (tools/reset.py)")
    a = ap.parse_args()

    rp = Replay(a)
    signal.signal(signal.SIGINT, lambda *_: setattr(rp, "stop", True))
    print(asyncio.run(rp.run()))


if __name__ == "__main__":
    main()
