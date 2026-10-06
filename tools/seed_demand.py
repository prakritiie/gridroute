"""Warm-start the live demand history from the real 2016 counts, aligned to the current hour of week, so
the forecaster (and the dashboard's forecast layer) work without waiting 28 days.

    python -m tools.seed_demand                 # real NYC clock; scale = demo requests / real requests
    python -m tools.seed_demand --scale 0.05

Replays seed themselves (sim/replay.py); the policy-lab runner calls seed_live() when it hands the city back
to the always-on demo.
"""
from __future__ import annotations

import argparse

import redis

from core import clock, config, demand


def seed_live(r: redis.Redis, rider_rate: float = 3.0, scale: float | None = None) -> tuple[int, float]:
    """Seed the 28 data days ending at the current hour of week. Returns (data end slot, demand scale)."""
    counts, hexes = demand.load_counts()
    nxt = clock.slot_of(clock.now(r))
    end = demand.aligned_end(nxt, counts.shape[1])
    demand.seed_history(r, counts, hexes, data_end=end, live_last=nxt - 1)
    # The demo's riders sample trips uniformly over the day, so match volume only on average.
    scale = scale or rider_rate * clock.SLOT_S / float(counts[:, -4 * 672:].sum(axis=0).mean())
    r.set("config:demand_scale", f"{scale:.6g}")
    return end, scale


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    ap.add_argument("--scale", type=float, help="live requests per real 2016 request (default: from rider rate)")
    ap.add_argument("--rider-rate", type=float, default=3.0, help="the demo's requests per second")
    a = ap.parse_args()
    r = redis.Redis.from_url(a.redis_url, decode_responses=True)
    end, scale = seed_live(r, a.rider_rate, a.scale)
    print(f"seeded 28 days ending {clock.slot_start(end - 1)} (data) -> live slot "
          f"{clock.slot_start(clock.slot_of(clock.now(r)))}; demand scale {scale:.4f}")


if __name__ == "__main__":
    main()
