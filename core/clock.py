"""What time is it in New York? The ETA features (hour of week, holidays) are NYC local time.

A replay publishes its simulated clock to Redis (`config:sim_clock`); otherwise it is the real current time
in America/New_York, whatever the host's own timezone is.

Demand is counted in 15-minute slots of that clock: slot 0 starts 2016-01-01 00:00 NYC, the same index the
training counts (ml/demand_data.py) use, so a replay's live history lines up with the data it was seeded from.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NYC = ZoneInfo("America/New_York")
KEY = "config:sim_clock"
EPOCH = datetime(2016, 1, 1)
SLOT_S = 900


def nyc_now() -> datetime:
    """Real current NYC wall time, naive (like the trip data's timestamps)."""
    return datetime.now(NYC).replace(tzinfo=None)


def set_sim_clock(r, sim0: datetime, speed: float, wall0_ms: int | None = None) -> None:
    r.set(KEY, json.dumps({"sim0": sim0.isoformat(), "speed": speed,
                           "wall0_ms": wall0_ms if wall0_ms is not None else int(time.time() * 1000)}))


def read(r) -> dict | None:
    """The published sim clock, or None when the real clock applies."""
    raw = r.get(KEY) if r is not None else None
    return json.loads(raw) if raw else None


def at(cfg: dict | None, wall_ms: float) -> datetime:
    """NYC time (sim or real) at a given host wall-clock instant."""
    if cfg:
        return datetime.fromisoformat(cfg["sim0"]) + timedelta(milliseconds=(wall_ms - cfg["wall0_ms"]) * cfg["speed"])
    return datetime.fromtimestamp(wall_ms / 1000, NYC).replace(tzinfo=None)


def now(r=None) -> datetime:
    """The replay's simulated NYC time if one is running, else the real NYC time."""
    cfg = read(r)
    return at(cfg, time.time() * 1000) if cfg else nyc_now()


def slot_of(t: datetime) -> int:
    """15-minute slot index of an NYC time (0 = 2016-01-01 00:00)."""
    return int((t - EPOCH).total_seconds() // SLOT_S)


def slot_start(slot: int) -> datetime:
    return EPOCH + timedelta(seconds=slot * SLOT_S)
