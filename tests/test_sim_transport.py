"""The simulator survives a gateway restart: re-registers, keeps seq monotonic, never loses a trip_done."""
import asyncio
import json
import socket

import numpy as np
import websockets

from sim.transport import WebSocketTransport
from sim.world import World


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeGateway:
    def __init__(self, port: int):
        self.port = port
        self.received: list[dict] = []
        self.server = None

    async def _handler(self, ws):
        async for raw in ws:
            self.received.append(json.loads(raw))

    async def start(self):
        self.server = await websockets.serve(self._handler, "127.0.0.1", self.port)

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    def of(self, kind):
        return [m for m in self.received if m["type"] == kind]


async def _until(cond, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.05)


def test_transport_heals_after_gateway_restart():
    async def scenario():
        port = _free_port()
        gw = FakeGateway(port)
        await gw.start()
        w = World(10, seed=1)
        tx = await WebSocketTransport.connect(f"ws://127.0.0.1:{port}", w.ids)
        await _until(lambda: len(gw.of("register")) == 1)

        await gw.stop()                                        # gateway goes away
        await _until(lambda: not tx.up)
        w.tick()
        await tx.send(w, w.due_pings(), 0)                     # dropped, not raised
        await tx.trip_done("d3", "r9")                         # queued
        assert tx.pending_done == [("d3", "r9")] and tx.dropped_pings > 0

        await gw.start()                                       # gateway is back
        await _until(lambda: tx.up and tx.reconnects == 1)
        await _until(lambda: len(gw.of("trip_done")) == 1)
        assert gw.of("register")[-1]["driver_ids"] == list(w.ids)
        assert gw.of("trip_done")[0] == {"type": "trip_done", "driver_id": "d3", "ride_id": "r9"}
        await tx.close()
        await gw.stop()

    asyncio.run(scenario())


def test_restarted_world_outranks_previous_session_seq():
    first = World(50, seed=0, seq_base=1_000_000)
    for _ in range(400):                                       # a long first session: ~100 pings per driver
        first.tick()
        first.due_pings()
    restarted = World(50, seed=0, seq_base=1_000_000 + 5_000)  # restarted 5 s later (ms base)
    restarted.tick()
    for _ in range(3):
        restarted.tick()
    b = restarted.due_pings()
    assert (b.seq > first.seq[b.idx]).all()
    assert np.all(first.seq > 1_000_000)
