"""Run the driver simulator in real time.

    python -m sim.main --drivers 5000                       # via the gateway: pings up, rides down, trip_done
    python -m sim.main --drivers 5000 --transport stream    # straight into stream:pings (no rides)
    python -m sim.main --kill-after 60 --kill-frac 0.3      # C4: silenced drivers must leave search in <= 35 s
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import signal
import time

import redis

from core import config
from sim.transport import StreamTransport, WebSocketTransport
from sim.world import ROAM, World


async def _maybe(x):
    if inspect.isawaitable(x):
        await x


async def run(a: argparse.Namespace) -> None:
    world = World(a.drivers, seed=a.seed, id_prefix=a.id_prefix, seq_base=int(time.time() * 1000))
    if a.transport == "ws":
        tx = await WebSocketTransport.connect(a.gateway, world.ids)
        target = a.gateway
    else:
        tx = StreamTransport(redis.Redis.from_url(a.redis_url, decode_responses=True))
        target = a.redis_url

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    print(f"sim: {a.drivers} drivers, 1 Hz ticks, pings every {world.ping_interval_s}s, {a.transport} -> {target}")
    start = time.monotonic()
    next_tick, last_report = start, start
    killed = False
    sent = assigned = completed = 0
    while not stop.is_set():
        now = time.monotonic()
        if a.duration and now - start >= a.duration:
            break
        if a.kill_after and not killed and now - start >= a.kill_after:
            print(f"sim: killed {len(world.kill(a.kill_frac))} drivers (they stop pinging)")
            killed = True

        for msg in tx.drain():
            assigned += world.assign(msg["driver_id"], msg["ride_id"], msg["pickup"], msg.get("dropoff"))
        for driver_id, ride_id in world.tick(1.0 * a.speedup):
            await _maybe(tx.trip_done(driver_id, ride_id))
            completed += 1
        batch = world.due_pings()
        await _maybe(tx.send(world, batch, int(time.time() * 1000)))
        sent += len(batch)

        if now - last_report >= 5:
            busy = int((world.mode != ROAM).sum())
            print(f"sim: t={now - start:5.0f}s  {sent / (now - last_report):6.0f} pings/s  live={int(world.alive.sum())}"
                  f"  busy={busy}  +assigned={assigned} +completed={completed}  conflicts={world.conflicts}"
                  + (f"  release_failures={tx.release_failures}" if hasattr(tx, "release_failures") else ""))
            sent = assigned = completed = 0
            last_report = now

        next_tick += 1.0
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, next_tick - time.monotonic()))
        except asyncio.TimeoutError:
            pass
    if hasattr(tx, "close"):
        await tx.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--drivers", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--speedup", type=float, default=1.0, help="sim seconds of driving per wall second")
    ap.add_argument("--duration", type=float, default=0, help="wall seconds to run; 0 = until Ctrl+C")
    ap.add_argument("--id-prefix", default="d", help="distinct prefixes let several sim processes share Redis")
    ap.add_argument("--transport", choices=["ws", "stream"], default="ws")
    ap.add_argument("--gateway", default=os.environ.get("GATEWAY_WS", "ws://localhost:8000/ws/driver"))
    ap.add_argument("--kill-after", type=float, default=0, help="wall seconds before silencing drivers")
    ap.add_argument("--kill-frac", type=float, default=0.3)
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
