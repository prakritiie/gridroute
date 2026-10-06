"""Demand-forecast features (spec §8.2): the ONE function training and the live forecaster both call.

    X = build(H, t, slot_start, hex_codes)

H is a (hexes x slots) matrix of 15-minute request counts; row i is hex i, column j is slot j. The function
returns features for predicting slot t for every hex, using only columns < t (what is known at that moment).
Training calls it for every slot of the training months; the forecaster calls it once per cycle with the
Redis history (demand_hist:{res7}). Same code, so no training/serving skew by construction.
"""
from __future__ import annotations

import numpy as np

from core.features import _HOLIDAYS

DEMAND_FEATURES = ["lag1", "last_hour", "lag96", "lag672", "ma4w", "how", "hour", "holiday", "hex"]
DEMAND_CATEGORICAL = ["hex"]
HISTORY = 4 * 672            # 28 days of 15-minute slots: the 4-week seasonal average needs all of it
SLOTS_PER_DAY, SLOTS_PER_WEEK = 96, 672


def build(H: np.ndarray, t: int, slot_start: np.datetime64, hex_codes: np.ndarray) -> np.ndarray:
    """Features (hexes x len(DEMAND_FEATURES), float32) to predict column t of H. Needs t >= HISTORY."""
    if t < HISTORY:
        raise ValueError(f"need {HISTORY} slots of history, got {t}")
    ts = np.datetime64(slot_start, "s")
    secs = ts.astype(np.int64)
    hour = (secs // 3600) % 24
    dow = ((secs // 86_400) + 3) % 7                                  # Monday = 0
    weeks = H[:, [t - k * SLOTS_PER_WEEK for k in (1, 2, 3, 4)]]
    n = H.shape[0]
    return np.column_stack([
        H[:, t - 1],                                                  # the slot just finished
        H[:, t - 4:t].sum(axis=1),                                    # the last hour
        H[:, t - SLOTS_PER_DAY],                                      # same slot yesterday
        H[:, t - SLOTS_PER_WEEK],                                     # same slot last week (seasonal naive)
        weeks.mean(axis=1),                                           # 4-week average of this slot
        np.full(n, dow * 24 + hour), np.full(n, hour),
        np.full(n, float(np.isin(ts.astype("datetime64[D]"), _HOLIDAYS))),
        hex_codes,
    ]).astype(np.float32)


def seasonal_naive(H: np.ndarray, t: int) -> np.ndarray:
    """Baseline 1: same slot last week."""
    return H[:, t - SLOTS_PER_WEEK]


def four_week_mean(H: np.ndarray, t: int) -> np.ndarray:
    """Baseline 2: average of the same slot over the last four weeks."""
    return H[:, [t - k * SLOTS_PER_WEEK for k in (1, 2, 3, 4)]].mean(axis=1)
