"""Clear simulated state between runs without breaking the running system.

    python -m tools.reset

FLUSHDB used to do this and took two things with it: the model registry pointer (model:eta:current), so
matchers fell back to B3 for a whole replay, and every stream's consumer group, so workers crashed on NOGROUP
and only came back through Docker restarts. This deletes simulation state only and empties the streams in
place, keeping their consumer groups. Model pointers, run history (run:*) and Grafana-facing metrics stay.
"""
from __future__ import annotations

import argparse

import redis

from core import config

STATE_PATTERNS = ("driver:*", "cell:*", "lock:driver:*", "ride:*", "explain:*", "demand_hist:*", "repos:*")
STATE_KEYS = ("active_cells", "offers", "rides:open", "hexes:snapshot", "kpis:latest",
              "config:max_attempts", "config:policy", "config:sim_clock", "run:current",
              "forecast:snapshot", "config:reposition", "config:demand_scale")
STREAMS = ("stream:pings", "stream:rides", "stream:events")


def reset_sim_state(r: redis.Redis) -> int:
    """Returns how many keys were removed. Streams are trimmed to empty, so their groups survive."""
    removed = 0
    for pattern in STATE_PATTERNS:
        batch = []
        for k in r.scan_iter(match=pattern, count=5000):
            batch.append(k)
            if len(batch) >= 5000:
                removed += r.unlink(*batch)
                batch = []
        if batch:
            removed += r.unlink(*batch)
    removed += r.unlink(*STATE_KEYS)
    for s in STREAMS:
        if r.exists(s):
            r.xtrim(s, maxlen=0)
    return removed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    a = ap.parse_args()
    n = reset_sim_state(redis.Redis.from_url(a.redis_url, decode_responses=True))
    print(f"reset: removed {n:,} keys; streams emptied, consumer groups and model pointers kept")


if __name__ == "__main__":
    main()
