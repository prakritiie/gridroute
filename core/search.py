"""Nearby-driver lookup: H3 ring search with the k+1 guard (spec §10.5), plus a brute-force reference for C3."""
from __future__ import annotations

import math
import time

import h3
import redis

from core import config

EARTH_R_M = 6_371_000.0


def now_ms() -> int:
    return int(time.time() * 1000)


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def rank_by_haversine(r: redis.Redis, lat: float, lng: float, ids: list[str]) -> list[tuple[str, float]]:
    """(driver_id, metres) nearest first, using each driver's last reported position."""
    if not ids:
        return []
    pipe = r.pipeline(transaction=False)
    for d in ids:
        pipe.hmget(f"driver:{d}", "lat", "lng")
    ranked = [(d, haversine_m(lat, lng, float(p[0]), float(p[1])))
              for d, p in zip(ids, pipe.execute()) if p[0] is not None]
    ranked.sort(key=lambda x: (x[1], x[0]))
    return ranked


def candidates(
    r: redis.Redis,
    lat: float,
    lng: float,
    need: int = config.CANDIDATES_NEED,
    kmax: int = config.RING_KMAX,
    max_age_ms: int = config.MAX_AGE_MS,
    now: int | None = None,
    guard: bool = True,
) -> list[tuple[str, float]]:
    """Up to `need` fresh available drivers nearest to (lat, lng).

    Hex rings aren't circles: a driver in a corner of ring k can be farther than one on the inner
    edge of ring k+1, so after reaching `need` drivers we search ONE more ring before ranking.
    `guard=False` exists only so a test can show what goes wrong without it.
    """
    now = now if now is not None else now_ms()
    origin = h3.latlng_to_cell(lat, lng, config.RES_DRIVER)
    ids: list[str] = []
    hit_k = None
    for k in range(kmax + 1):
        pipe = r.pipeline(transaction=False)
        for c in h3.grid_ring(origin, k):
            pipe.zrangebyscore(f"cell:{c}", now - max_age_ms, "+inf")     # fresh drivers only
        for part in pipe.execute():
            ids.extend(part)
        if len(ids) >= need and hit_k is None:
            hit_k = k
        if hit_k is not None and (not guard or k >= hit_k + 1):
            break
    return rank_by_haversine(r, lat, lng, ids)[:need]


def brute_force(
    r: redis.Redis, lat: float, lng: float, need: int, radius_m: float,
    max_age_ms: int = config.MAX_AGE_MS, now: int | None = None,
) -> list[tuple[str, float]]:
    """Reference answer: scan every indexed driver, keep fresh ones within radius_m, rank by distance."""
    now = now if now is not None else now_ms()
    ids = []
    for ck in r.scan_iter(match="cell:*", count=1000, _type="ZSET"):
        ids.extend(r.zrangebyscore(ck, now - max_age_ms, "+inf"))
    return [x for x in rank_by_haversine(r, lat, lng, ids) if x[1] <= radius_m][:need]
