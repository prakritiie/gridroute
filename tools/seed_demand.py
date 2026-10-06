"""Warm-start the live demand history from the real 2016 counts, aligned to the current hour of week, so
the forecaster (and the dashboard's forecast layer) work without waiting 28 days.

    python -m tools.seed_demand                 # real NYC clock; scale = demo requests / real requests
    python -m tools.seed_demand --scale 0.05

Replays seed themselves (sim/replay.py); this is for the always-on demo.
"""
from __future__ import annotations

import argparse

import redis

from core import clock, config, demand


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    ap.add_argument("--scale", type=float, help="live requests per real 2016 request (default: from rider rate)")
    ap.add_argument("--rider-rate", type=float, default=3.0, help="the demo's requests per second")
    a = ap.parse_args()
    r = redis.Redis.from_url(a.redis_url, decode_responses=True)
    counts, hexes = demand.load_counts()
    nxt = clock.slot_of(clock.now(r))
    end = demand.aligned_end(nxt, counts.shape[1])
    demand.seed_history(r, counts, hexes, data_end=end, live_last=nxt - 1)
    # The demo's riders sample trips uniformly over the day, so match volume only on average.
    scale = a.scale or a.rider_rate * clock.SLOT_S / float(counts[:, -4 * 672:].sum(axis=0).mean())
    r.set("config:demand_scale", f"{scale:.6g}")
    print(f"seeded {len(hexes)} hexes x 28 days ending {clock.slot_start(end - 1)} (data) -> live slot "
          f"{clock.slot_start(nxt)}; demand scale {scale:.4f}")


if __name__ == "__main__":
    main()
