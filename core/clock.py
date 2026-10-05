"""What time is it in New York? The ETA features (hour of week, holidays) are NYC local time.

A replay publishes its simulated clock to Redis (`config:sim_clock`); otherwise it is the real current time
in America/New_York, whatever the host's own timezone is.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NYC = ZoneInfo("America/New_York")
KEY = "config:sim_clock"


def nyc_now() -> datetime:
    """Real current NYC wall time, naive (like the trip data's timestamps)."""
    return datetime.now(NYC).replace(tzinfo=None)


def set_sim_clock(r, sim0: datetime, speed: float, wall0_ms: int | None = None) -> None:
    r.set(KEY, json.dumps({"sim0": sim0.isoformat(), "speed": speed,
                           "wall0_ms": wall0_ms if wall0_ms is not None else int(time.time() * 1000)}))


def now(r=None) -> datetime:
    """The replay's simulated NYC time if one is running, else the real NYC time."""
    raw = r.get(KEY) if r is not None else None
    if raw:
        c = json.loads(raw)
        return datetime.fromisoformat(c["sim0"]) + timedelta(
            milliseconds=(time.time() * 1000 - c["wall0_ms"]) * c["speed"])
    return nyc_now()
