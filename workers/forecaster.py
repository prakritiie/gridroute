"""Forecaster (spec §8.2, M3): requests expected per res-7 hex in the current 15-minute slot.

    python -m workers.forecaster

Recomputes whenever the aggregator closes a slot (demand_hist:last_slot moves) or a new model version is
published, checking every second. One LightGBM call for all hexes (~ms). Writes:

  forecast:snapshot   {"slot", "slot_start", "version", "scale", "hexes": {res7: requests}}   full-city units
  ch:forecast         the same JSON, for the dashboard and the repositioner

No published model or no history yet: writes nothing (the repositioner then does nothing).
"""
from __future__ import annotations

import argparse
import json
import signal
import threading

import redis

from core import clock, config, metrics
from core.demand import LAST_SLOT, DemandRegistry, read_history

SNAPSHOT, CHANNEL = "forecast:snapshot", "ch:forecast"


class Forecaster:
    def __init__(self, r: redis.Redis, registry: DemandRegistry | None = None):
        self.r = r
        self.registry = registry or DemandRegistry(r)
        self.done: tuple[str, int] | None = None          # (version, last_slot) of the last forecast

    def step(self) -> dict | None:
        model = self.registry.current()
        last = self.r.get(LAST_SLOT)
        if model is None or last is None or self.done == (model.version, int(last)):
            return None
        got = read_history(self.r, model.hexes)
        if got is None:
            return None
        H, last = got
        slot = last + 1
        fc = model.forecast(H, slot)
        out = {"slot": slot, "slot_start": clock.slot_start(slot).isoformat(), "version": model.version,
               "scale": float(self.r.get("config:demand_scale") or 1.0),
               "hexes": {h: round(v, 2) for h, v in fc.items()}}
        body = json.dumps(out, separators=(",", ":"))
        pipe = self.r.pipeline(transaction=False)
        pipe.set(SNAPSHOT, body)
        pipe.publish(CHANNEL, body)
        pipe.execute()
        self.done = (model.version, last)
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    a = ap.parse_args()
    metrics.serve_from_env()
    fc = Forecaster(redis.Redis.from_url(a.redis_url, decode_responses=True))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print("forecaster: forecasting each new 15-min slot", flush=True)
    while not stop.is_set():
        try:
            out = fc.step()
            if out:
                total = sum(out["hexes"].values())
                print(f"forecaster: slot {out['slot_start']} {out['version']}: {total:,.0f} requests city-wide",
                      flush=True)
        except redis.RedisError as e:
            print(f"forecaster: {e}", flush=True)
        stop.wait(1.0)


if __name__ == "__main__":
    main()
