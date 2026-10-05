"""How simulated drivers reach the system.

WebSocketTransport  the real path: one socket per sim process carries batched pings up and assignments down
                    (spec §11 WS /ws/driver). Survives gateway restarts like a real driver app would.
StreamTransport     writes straight to stream:pings, bypassing the gateway. Ingest-only tests and benchmarks;
                    drivers never receive rides on this path.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import redis
import websockets
from websockets.exceptions import ConnectionClosed

from sim.world import PingBatch, World

PINGS_STREAM = "stream:pings"
PINGS_MAXLEN = 1_000_000


class StreamTransport:
    def __init__(self, r: redis.Redis, stream: str = PINGS_STREAM):
        self.r = r
        self.stream = stream

    def send(self, world: World, batch: PingBatch, t_ms: int) -> None:
        if not len(batch):
            return
        pipe = self.r.pipeline(transaction=False)
        for d, lat, lng, seq in zip(world.ids[batch.idx], batch.lat, batch.lng, batch.seq):
            pipe.xadd(
                self.stream,
                {"d": str(d), "lat": float(lat), "lng": float(lng), "seq": int(seq), "t": t_ms},
                maxlen=PINGS_MAXLEN,
                approximate=True,
            )
        pipe.execute()

    def drain(self) -> list[dict]:
        return []

    def trip_done(self, driver_id: str, ride_id: str) -> None:
        pass

    def accept(self, driver_id: str, ride_id: str) -> None:
        pass

    def drain_rejected(self) -> list[tuple[str, str]]:
        return []


class WebSocketTransport:
    """Gateway connection that heals itself.

    While disconnected: pings are dropped (drivers simply age out of search and come back on reconnect),
    trip_done messages are queued and resent after reconnecting (losing one would strand the driver
    until its lease expires), and reconnects back off from 0.5 s to 8 s.
    """

    def __init__(self, url: str, driver_ids):
        self.url = url
        self.ids = [str(d) for d in driver_ids]
        self.ws = None
        self.inbox: list[dict] = []
        self.rejected: list[tuple[str, str]] = []      # accepts that came back ok=false: the offer had lapsed
        self.pending_done: list[tuple[str, str]] = []
        self.release_failures = 0
        self.dropped_pings = 0
        self.reconnects = 0
        self._reader: asyncio.Task | None = None
        self._healer: asyncio.Task | None = None

    @classmethod
    async def connect(cls, url: str, driver_ids) -> "WebSocketTransport":
        t = cls(url, driver_ids)
        await t._connect_until_up()
        return t

    @property
    def up(self) -> bool:
        return self.ws is not None

    async def _open(self) -> None:
        ws = await websockets.connect(self.url, max_size=8 * 2**20, ping_interval=20)
        await ws.send(json.dumps({"type": "register", "driver_ids": self.ids}))
        self.ws = ws
        self._reader = asyncio.create_task(self._read(ws))
        queued, self.pending_done = self.pending_done, []
        for driver_id, ride_id in queued:
            await self.trip_done(driver_id, ride_id)
        if queued:
            print(f"sim: resent {len(queued)} trip_done queued while offline", flush=True)

    async def _connect_until_up(self) -> None:
        delay = 0.5
        while True:
            try:
                await self._open()
                return
            except (OSError, ConnectionClosed, websockets.InvalidHandshake, asyncio.TimeoutError) as e:
                print(f"sim: gateway unreachable ({type(e).__name__}), retrying in {delay:.1f}s", flush=True)
                await asyncio.sleep(delay)
                delay = min(8.0, delay * 2)

    def _lost(self) -> None:
        if self.ws is None:
            return
        self.ws = None
        print("sim: lost the gateway, reconnecting", flush=True)
        if self._healer is None or self._healer.done():
            self._healer = asyncio.create_task(self._heal())

    async def _heal(self) -> None:
        await self._connect_until_up()
        self.reconnects += 1
        print(f"sim: reconnected, re-registered {len(self.ids)} drivers", flush=True)

    async def _read(self, ws) -> None:
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "assignment":
                    self.inbox.append(msg)
                elif msg.get("type") == "trip_done_ack" and not msg.get("ok"):
                    self.release_failures += 1
                elif msg.get("type") == "accept_ack" and not msg.get("ok"):
                    self.rejected.append((msg.get("driver_id", ""), msg.get("ride_id", "")))
        except ConnectionClosed:
            pass
        if ws is self.ws:
            self._lost()

    async def send(self, world: World, batch: PingBatch, t_ms: int) -> None:
        if not len(batch):
            return
        if not self.up:
            self.dropped_pings += len(batch)
            return
        pings = [[str(d), float(la), float(ln), int(s), t_ms]
                 for d, la, ln, s in zip(world.ids[batch.idx], batch.lat, batch.lng, batch.seq)]
        try:
            await self.ws.send(json.dumps({"type": "pings", "pings": pings}))
        except ConnectionClosed:
            self.dropped_pings += len(batch)
            self._lost()

    def drain(self) -> list[dict]:
        msgs, self.inbox = self.inbox, []
        return msgs

    async def accept(self, driver_id: str, ride_id: str) -> None:
        """Take an offer. If the socket is down the offer simply lapses and the ride is re-queued."""
        if self.up:
            try:
                await self.ws.send(json.dumps({"type": "accept", "driver_id": driver_id, "ride_id": ride_id}))
            except ConnectionClosed:
                self._lost()

    def drain_rejected(self) -> list[tuple[str, str]]:
        out, self.rejected = self.rejected, []
        return out

    async def trip_done(self, driver_id: str, ride_id: str) -> None:
        if not self.up:
            self.pending_done.append((driver_id, ride_id))
            return
        try:
            await self.ws.send(json.dumps({"type": "trip_done", "driver_id": driver_id, "ride_id": ride_id}))
        except ConnectionClosed:
            self.pending_done.append((driver_id, ride_id))
            self._lost()

    async def close(self) -> None:
        for task in (self._reader, self._healer):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self.ws is not None:
            await self.ws.close()
