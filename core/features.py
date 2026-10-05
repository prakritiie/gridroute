"""ETA features: the ONE function both training (ml/) and serving (the matcher) call (spec §8.1, §14).

Training/serving skew is the classic way a model that tests well dispatches badly, so there is no second
implementation anywhere: tests/test_features.py asserts row-by-row identity between the offline (batch) and
online (one request at a time) paths.

Every feature is known at dispatch time. Fare, tip, payment type, passenger count and drop-off time are
deliberately absent (they are unknown when a driver is chosen, or they leak the answer); a test enforces it.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

import h3
import holidays
import numpy as np

from core import config

FEATURES = [
    "osrm_s", "osrm_m", "straight_m", "osrm_speed", "detour", "bearing",
    "pickup_lat", "pickup_lng", "dropoff_lat", "dropoff_lng",
    "pickup_r8", "dropoff_r8", "how", "hour", "holiday", "pickup_airport", "dropoff_airport",
]
CATEGORICAL = ["pickup_r8", "dropoff_r8", "pickup_airport", "dropoff_airport"]
INPUTS = ["pickup_lat", "pickup_lng", "dropoff_lat", "dropoff_lng", "pickup_ts", "osrm_s", "osrm_m"]
FORBIDDEN = ("fare", "tip", "payment", "passenger", "dropoff_ts", "duration", "total", "toll", "vendor")

# (lat_min, lat_max, lng_min, lng_max); same boxes as the data card
AIRPORTS = {1: (40.620, 40.666, -73.835, -73.745), 2: (40.764, 40.790, -73.891, -73.854)}   # 1 JFK, 2 LGA
_HOLIDAYS = np.array(sorted(np.datetime64(d, "D") for y in (2015, 2016, 2017) for d in holidays.US(subdiv="NY", years=y)))


class Vocab:
    """res-8 hex -> small integer code for LightGBM's categorical splits. Learned on training data only."""

    def __init__(self, codes: dict[str, int]):
        self.codes = codes

    @classmethod
    def fit(cls, lat: np.ndarray, lng: np.ndarray, top: int = 4000, min_count: int = 50) -> "Vocab":
        counts = Counter(h3.latlng_to_cell(a, b, config.RES_ETA) for a, b in zip(lat.tolist(), lng.tolist()))
        keep = [c for c, n in counts.most_common(top) if n >= min_count]
        return cls({c: i for i, c in enumerate(sorted(keep))})

    def encode(self, lat: np.ndarray, lng: np.ndarray) -> np.ndarray:
        # unknown hexes -> NaN, which LightGBM treats as missing (its own branch), not as a real category
        return np.array([self.codes.get(h3.latlng_to_cell(a, b, config.RES_ETA), np.nan)
                         for a, b in zip(lat.tolist(), lng.tolist())], dtype=np.float32)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.codes))

    @classmethod
    def load(cls, path: Path) -> "Vocab":
        return cls(json.loads(path.read_text()))


def _haversine_m(lat1, lng1, lat2, lng2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6_371_000 * np.arcsin(np.sqrt(a))


def _bearing(lat1, lng1, lat2, lng2):
    p1, p2, dl = np.radians(lat1), np.radians(lat2), np.radians(lng2 - lng1)
    y = np.sin(dl) * np.cos(p2)
    x = np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl)
    return (np.degrees(np.arctan2(y, x)) + 360) % 360


def _airport(lat, lng):
    out = np.zeros(len(lat), dtype=np.float32)
    for code, (a, b, c, d) in AIRPORTS.items():
        out[(lat >= a) & (lat <= b) & (lng >= c) & (lng <= d)] = code
    return out


def build(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng, pickup_ts, osrm_s, osrm_m, vocab: Vocab) -> np.ndarray:
    """Feature matrix (n x len(FEATURES), float32) from dispatch-time inputs.

    pickup_ts: local NYC wall time (naive), as numpy datetime64 or anything np.asarray(..., 'datetime64[s]')
    accepts. osrm_s / osrm_m: free-flow OSRM time and distance for the leg (NaN if no route).
    """
    plat, plng = np.asarray(pickup_lat, dtype=np.float64), np.asarray(pickup_lng, dtype=np.float64)
    dlat, dlng = np.asarray(dropoff_lat, dtype=np.float64), np.asarray(dropoff_lng, dtype=np.float64)
    o_s, o_m = np.asarray(osrm_s, dtype=np.float64), np.asarray(osrm_m, dtype=np.float64)
    ts = np.asarray(pickup_ts, dtype="datetime64[s]")
    secs = ts.astype(np.int64)
    days = secs // 86_400
    hour = (secs // 3600) % 24
    dow = (days + 3) % 7                                   # 1970-01-01 was a Thursday -> Monday = 0
    straight = _haversine_m(plat, plng, dlat, dlng)
    with np.errstate(divide="ignore", invalid="ignore"):
        speed = np.where(o_s > 0, o_m / o_s, np.nan)
        detour = np.where(straight > 0, o_m / straight, np.nan)
    cols = {
        "osrm_s": o_s, "osrm_m": o_m, "straight_m": straight, "osrm_speed": speed, "detour": detour,
        "bearing": _bearing(plat, plng, dlat, dlng),
        "pickup_lat": plat, "pickup_lng": plng, "dropoff_lat": dlat, "dropoff_lng": dlng,
        "pickup_r8": vocab.encode(plat, plng), "dropoff_r8": vocab.encode(dlat, dlng),
        "how": dow * 24 + hour, "hour": hour,
        "holiday": np.isin(ts.astype("datetime64[D]"), _HOLIDAYS).astype(np.float32),
        "pickup_airport": _airport(plat, plng), "dropoff_airport": _airport(dlat, dlng),
    }
    return np.column_stack([np.asarray(cols[f], dtype=np.float32) for f in FEATURES])


def build_one(pickup_lat: float, pickup_lng: float, dropoff_lat: float, dropoff_lng: float, pickup_ts,
              osrm_s: float, osrm_m: float, vocab: Vocab) -> np.ndarray:
    """The online path: a single request (what the matcher builds per candidate). Same code underneath."""
    nan = math.nan
    return build([pickup_lat], [pickup_lng], [dropoff_lat], [dropoff_lng], [np.datetime64(pickup_ts, "s")],
                 [nan if osrm_s is None else osrm_s], [nan if osrm_m is None else osrm_m], vocab)[0]

