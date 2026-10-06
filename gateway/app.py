"""API gateway (spec §11, arrows 1-4, 7, 8). Stateless apart from its own sockets, so replicas scale freely.

    uvicorn gateway.app:app --port 8000

WS /ws/driver   up:   {"type":"register","driver_ids":[..]}
                      {"type":"pings","pings":[[d,lat,lng,seq,t_ms], ...]}       batched, ~every tick
                      {"type":"accept","driver_id":..,"ride_id":..}           within the 15 s offer lease
                      {"type":"trip_done","driver_id":..,"ride_id":..}
                down: {"type":"assignment","driver_id","ride_id","pickup":[lat,lng],"dropoff"?,"eta_s"}
                      {"type":"accept_ack","driver_id","ride_id","ok"}       ok=false: the offer lapsed
                      {"type":"trip_done_ack","ride_id","ok"}
                      {"type":"reposition","driver_id","target":[lat,lng],"to"}   drive there if still idle
WS /ws/ops      down: {"type":"hexes"|"kpis"|"match"|"forecast"|"reposition","data":...}; on connect the recent matches and the
                      latest kpis and hexes are sent first. Each client has a bounded queue: a slow
                      browser drops frames instead of slowing everyone else.
POST /rides     {lat,lng[,dropoff_lat,dropoff_lng]} -> 202 {ride_id}
GET  /rides/{id}, GET /rides/{id}/explain, GET /hexes?res=9|8|7, GET /kpis, GET /healthz, GET /metrics
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from core import config, metrics
from core.config import LAT_MAX, LAT_MIN, LNG_MAX, LNG_MIN
from core.scripts import AsyncScripts, ride_key

log = logging.getLogger("gateway")

PINGS, RIDES, EVENTS = "stream:pings", "stream:rides", "stream:events"
OFFERS, HEXES, KPIS = "ch:offers", "ch:hexes", "ch:kpis"
FORECAST, REPOSITION = "ch:forecast", "ch:reposition"
RIDE_TTL_S = 24 * 3600
OPS_QUEUE = 8           # frames buffered per dashboard client before the oldest is dropped
RECENT_MATCHES = 40


def in_nyc(lat: float, lng: float) -> bool:
    return LAT_MIN <= lat <= LAT_MAX and LNG_MIN <= lng <= LNG_MAX


def now_ms() -> int:
    return int(time.time() * 1000)


class DriverSockets:
    """Which socket on THIS replica holds which driver. Offers for drivers elsewhere are ignored here."""

    def __init__(self):
        self.by_driver: dict[str, WebSocket] = {}

    def register(self, ws: WebSocket, ids: list[str]) -> None:
        for d in ids:
            self.by_driver[d] = ws

    def drop(self, ws: WebSocket) -> None:
        for d in [d for d, w in self.by_driver.items() if w is ws]:
            del self.by_driver[d]

    async def deliver(self, msg: dict) -> bool:
        ws = self.by_driver.get(msg.get("driver_id", ""))
        if ws is None:
            return False
        try:
            await ws.send_json(msg)
            return True
        except Exception:  # noqa: BLE001 - socket died between lookup and send
            self.drop(ws)
            return False


class OpsHub:
    """Broadcast to dashboard sockets. Frames are serialised once; each client drains its own queue."""

    def __init__(self):
        self.clients: set[asyncio.Queue] = set()
        self.recent: deque[str] = deque(maxlen=RECENT_MATCHES)

    def broadcast(self, frame: str) -> None:
        for q in self.clients:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(frame)


def _frame(kind: str, data: str) -> str:
    return '{"type":"%s","data":%s}' % (kind, data)


async def _fan_out(r: aioredis.Redis, sockets: DriverSockets, ops: OpsHub) -> None:
    pubsub = r.pubsub()
    await pubsub.subscribe(OFFERS, HEXES, KPIS, FORECAST, REPOSITION)
    try:
        async for m in pubsub.listen():
            if m["type"] != "message":
                continue
            channel, data = m["channel"], m["data"]
            if channel == OFFERS:
                with contextlib.suppress(ValueError):
                    await sockets.deliver(json.loads(data))
                frame = _frame("match", data)
                ops.recent.append(frame)
                ops.broadcast(frame)
            elif channel == HEXES:
                ops.broadcast(_frame("hexes", data))
            elif channel == KPIS:
                ops.broadcast(_frame("kpis", data))
            elif channel == FORECAST:
                ops.broadcast(_frame("forecast", data))
            elif channel == REPOSITION:
                with contextlib.suppress(ValueError):
                    batch = json.loads(data)
                    for m in batch.get("moves", []):
                        await sockets.deliver({"type": "reposition", "driver_id": m["driver_id"],
                                               "target": [m["lat"], m["lng"]], "to": m["to"]})
                    ops.broadcast(_frame("reposition", json.dumps(
                        {"t": batch.get("t"), "n": len(batch.get("moves", [])),
                         "flows": [[m["from"], m["to"]] for m in batch.get("moves", [])]}, separators=(",", ":"))))
    finally:
        await pubsub.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI):
    r = aioredis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    app.state.r = r
    app.state.scripts = AsyncScripts(r)
    app.state.sockets = DriverSockets()
    app.state.ops = OpsHub()
    fan_out = asyncio.create_task(_fan_out(r, app.state.sockets, app.state.ops))
    yield
    fan_out.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await fan_out
    await r.aclose()


app = FastAPI(title="GridRoute gateway", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---- REST -----------------------------------------------------------------
class RideRequest(BaseModel):
    lat: float = Field(ge=LAT_MIN, le=LAT_MAX)
    lng: float = Field(ge=LNG_MIN, le=LNG_MAX)
    dropoff_lat: float | None = Field(None, ge=LAT_MIN, le=LAT_MAX)
    dropoff_lng: float | None = Field(None, ge=LNG_MIN, le=LNG_MAX)


@app.post("/rides", status_code=202)
async def create_ride(req: RideRequest) -> dict:
    r: aioredis.Redis = app.state.r
    ride_id = uuid.uuid4().hex[:16]
    t = now_ms()
    ride = {"status": "queued", "lat": req.lat, "lng": req.lng, "t_req": t, "attempts": 0}
    if req.dropoff_lat is not None and req.dropoff_lng is not None:
        ride.update(dlat=req.dropoff_lat, dlng=req.dropoff_lng)
    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(ride_key(ride_id), mapping=ride)
        pipe.expire(ride_key(ride_id), RIDE_TTL_S)
        pipe.xadd(RIDES, {"ride_id": ride_id, "lat": req.lat, "lng": req.lng, "t": t}, maxlen=100_000, approximate=True)
        await pipe.execute()
    metrics.rides_requested.inc()
    return {"ride_id": ride_id}


@app.get("/rides/{ride_id}")
async def get_ride(ride_id: str) -> dict:
    ride = await app.state.r.hgetall(ride_key(ride_id))
    if not ride:
        raise HTTPException(404, "ride not found")
    return {"ride_id": ride_id, **ride}


@app.get("/rides/{ride_id}/explain")
async def explain(ride_id: str) -> dict:
    """Why this driver: every candidate with straight-line metres, OSRM and model seconds, and the cost used."""
    raw = await app.state.r.get(f"explain:{ride_id}")
    if not raw:
        raise HTTPException(404, "no explanation for this ride (unmatched, or older than an hour)")
    return json.loads(raw)


@app.get("/hexes")
async def hexes(res: int = 9) -> dict:
    """Latest aggregator snapshot at one resolution: cells = [[h3, supply, demand], ...]."""
    if res not in (config.RES_DRIVER, config.RES_ETA, config.RES_DEMAND):
        raise HTTPException(422, "res must be 9, 8 or 7")
    snap = await app.state.r.get("hexes:snapshot")
    if not snap:
        return {"res": res, "t": None, "window_s": None, "cells": []}
    data = json.loads(snap)
    return {"res": res, "t": data["t"], "window_s": data["window_s"], "cells": data["res"][str(res)]}


@app.get("/kpis")
async def kpis() -> dict:
    k = await app.state.r.get("kpis:latest")
    return json.loads(k) if k else {}


@app.get("/healthz")
async def healthz() -> dict:
    await app.state.r.ping()
    return {"ok": True}


@app.get("/metrics")
async def prometheus_metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---- driver socket -----------------------------------------------------------
def _valid_ping(p) -> tuple | None:
    try:
        d, lat, lng, seq, t = str(p[0]), float(p[1]), float(p[2]), int(p[3]), int(p[4])
    except (TypeError, ValueError, IndexError):
        return None
    return (d, lat, lng, seq, t) if d and in_nyc(lat, lng) else None


@app.websocket("/ws/driver")
async def driver_socket(ws: WebSocket) -> None:
    await ws.accept()
    metrics.driver_sockets.inc()
    r: aioredis.Redis = app.state.r
    sockets: DriverSockets = app.state.sockets
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "pings":
                pings = [p for p in map(_valid_ping, msg.get("pings", [])) if p]
                if pings:
                    async with r.pipeline(transaction=False) as pipe:
                        for d, lat, lng, seq, t in pings:
                            pipe.xadd(PINGS, {"d": d, "lat": lat, "lng": lng, "seq": seq, "t": t},
                                      maxlen=1_000_000, approximate=True)
                        await pipe.execute()
                    metrics.pings_received.inc(len(pings))
            elif kind == "register":
                sockets.register(ws, [str(d) for d in msg.get("driver_ids", [])])
            elif kind == "event":
                # Simulator-observed facts the system can't see itself (pickup arrival, drop-off, fleet
                # totals), recorded with everything else for the evaluation report.
                ev = msg.get("event") or {}
                fields = {str(k)[:32]: v for k, v in list(ev.items())[:24]
                          if isinstance(v, (str, int, float)) and not isinstance(v, bool)}
                if fields.get("type"):
                    await r.xadd(EVENTS, fields, maxlen=1_000_000, approximate=True)
            elif kind == "accept":
                d, ride = str(msg.get("driver_id", "")), str(msg.get("ride_id", ""))
                res = await app.state.scripts.accept_offer(d, ride, now_ms())
                if res == 1:
                    await r.xadd(EVENTS, {"type": "accept", "ride": ride, "driver": d, "t": now_ms()},
                                 maxlen=1_000_000, approximate=True)
                await ws.send_json({"type": "accept_ack", "ride_id": ride, "driver_id": d, "ok": res == 1})
            elif kind == "trip_done":
                d, ride = str(msg.get("driver_id", "")), str(msg.get("ride_id", ""))
                ok = await app.state.scripts.release_driver(d, ride, now_ms())
                if ok:
                    await r.xadd(EVENTS, {"type": "complete", "ride": ride, "driver": d, "t": now_ms()},
                                 maxlen=1_000_000, approximate=True)
                await ws.send_json({"type": "trip_done_ack", "ride_id": ride, "ok": ok})
    except WebSocketDisconnect:
        pass
    finally:
        sockets.drop(ws)
        metrics.driver_sockets.dec()


# ---- ops (dashboard) socket ----------------------------------------------------
@app.websocket("/ws/ops")
async def ops_socket(ws: WebSocket) -> None:
    await ws.accept()
    ops: OpsHub = app.state.ops
    q: asyncio.Queue = asyncio.Queue(maxsize=OPS_QUEUE)
    ops.clients.add(q)                                  # subscribe before reading state: no gap to lose frames in
    metrics.ops_sockets.inc()
    snap, k, fc = await app.state.r.mget("hexes:snapshot", "kpis:latest", "forecast:snapshot")
    for frame in list(ops.recent):
        await ws.send_text(frame)
    if k:
        await ws.send_text(_frame("kpis", k))
    if snap:
        await ws.send_text(_frame("hexes", snap))
    if fc:
        await ws.send_text(_frame("forecast", fc))

    async def wait_closed() -> None:                    # the dashboard never sends; this notices it leaving
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            while True:
                await ws.receive_text()

    closed = asyncio.create_task(wait_closed())
    try:
        while True:
            getter = asyncio.create_task(q.get())
            done, _ = await asyncio.wait({getter, closed}, return_when=asyncio.FIRST_COMPLETED)
            if getter not in done:
                getter.cancel()
                break
            await ws.send_text(getter.result())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        ops.clients.discard(q)
        closed.cancel()
        metrics.ops_sockets.dec()
