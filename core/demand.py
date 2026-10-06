"""Demand model at serve time (spec §8.2): load a published version, read the live history, forecast.

    model = DemandRegistry(r).current()          # follows model:demand:current, hot-reloads
    H, last = read_history(r, model.hexes)       # (hexes x HISTORY) from demand_hist:{res7}, or None
    model.forecast(H, last + 1)                  # {res7: requests expected in that 15-min slot}

Features come from core.demand_features.build, the function training used. Forecasts are in full-city
units (the aggregator divides live counts by config:demand_scale), so a consumer multiplies by the scale.

seed_history() warm-starts the history from the training counts: a replay seeds the 28 days before its
window; the live demo seeds the latest data weeks aligned to the current hour of week.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import redis

from core import clock, config
from core.demand_features import HISTORY, SLOTS_PER_WEEK, build

MODELS = Path(config.__file__).resolve().parent.parent / "models" / "demand"
POINTER = "model:demand:current"
LAST_SLOT = "demand_hist:last_slot"
COUNTS = config.DATA_DIR / "features" / "demand_counts.npz"
RELOAD_S = 30.0


class DemandModel:
    def __init__(self, version: str, booster: lgb.Booster, hexes: list[str]):
        self.version, self.booster, self.hexes = version, booster, hexes
        self.codes = np.arange(len(hexes), dtype=np.float32)

    @classmethod
    def load(cls, version: str, root: Path = MODELS) -> "DemandModel":
        d = root / version
        booster = lgb.Booster(model_file=str(d / "model.txt"))
        return cls(version, booster, json.loads((d / "hexes.json").read_text(encoding="utf-8")))

    def forecast(self, H: np.ndarray, slot: int) -> dict[str, float]:
        """Expected requests per hex in `slot`, given H = the HISTORY slots just before it."""
        X = build(H, H.shape[1], np.datetime64(clock.slot_start(slot)), self.codes)
        return {h: float(max(p, 0.0)) for h, p in zip(self.hexes, self.booster.predict(X))}


class DemandRegistry:
    """Follows the Redis pointer; reloads when it changes (checked at most every RELOAD_S)."""

    def __init__(self, r: redis.Redis, root: Path = MODELS):
        self.r, self.root = r, root
        self.model: DemandModel | None = None
        self.checked = 0.0

    def current(self) -> DemandModel | None:
        now = time.monotonic()
        if self.model is not None and now - self.checked < RELOAD_S:
            return self.model
        self.checked = now
        version = self.r.get(POINTER)
        if version and (self.model is None or self.model.version != version):
            try:
                self.model = DemandModel.load(version, self.root)
                print(f"demand: loaded model {version}", flush=True)
            except (OSError, ValueError, lgb.basic.LightGBMError) as e:
                print(f"demand: could not load {version} ({e}); keeping {getattr(self.model, 'version', None)}",
                      flush=True)
        return self.model


def read_history(r: redis.Redis, hexes: list[str]) -> tuple[np.ndarray, int] | None:
    """(hexes x HISTORY) counts ending at demand_hist:last_slot, or None while no hex has a full history.
    A model hex with a short live history (never seen since the warm start) counts as zeros."""
    last = r.get(LAST_SLOT)
    if last is None:
        return None
    pipe = r.pipeline(transaction=False)
    for h in hexes:
        pipe.lrange(f"demand_hist:{h}", -HISTORY, -1)
    rows = pipe.execute()
    if not any(len(x) >= HISTORY for x in rows):
        return None
    H = np.zeros((len(hexes), HISTORY), dtype=np.float32)
    for i, x in enumerate(rows):
        if len(x) >= HISTORY:
            H[i] = np.asarray(x, dtype=np.float32)
    return H, int(last)


def seed_history(r: redis.Redis, counts: np.ndarray, hexes: list[str], data_end: int, live_last: int) -> None:
    """Write counts[:, data_end-HISTORY : data_end] as the live history whose last slot is live_last.
    Replaces any history already there (one transaction, so the aggregator never sees half of it)."""
    if data_end < HISTORY or data_end > counts.shape[1]:
        raise ValueError(f"data_end {data_end} outside [{HISTORY}, {counts.shape[1]}]")
    old = [k for k in r.scan_iter("demand_hist:*", count=1000)]
    pipe = r.pipeline(transaction=True)
    if old:
        pipe.delete(*old)
    window = counts[:, data_end - HISTORY:data_end]
    for h, row in zip(hexes, window):
        pipe.rpush(f"demand_hist:{h}", *[f"{v:.6g}" for v in row])
    pipe.set(LAST_SLOT, live_last)
    pipe.execute()


def load_counts(path: Path = COUNTS) -> tuple[np.ndarray, list[str]]:
    z = np.load(path)
    return z["counts"], [str(h) for h in z["hexes"]]


def aligned_end(live_next: int, n_slots: int) -> int:
    """The latest data slot with the same hour-of-week as live_next (the slot about to be forecast)."""
    return n_slots - ((n_slots - live_next) % SLOTS_PER_WEEK)
