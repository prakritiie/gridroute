"""Nearby-driver lookup: H3 ring search with the k+1 guard (spec §10.5), plus a brute-force reference for C3."""
from __future__ import annotations

import math
import time

import h3
import redis

from core import config
from core.scripts import LUA_DIR

EARTH_R_M = 6_371_000.0
# Max distance from a res-9 cell centre to its boundary in the NYC service area is 206.0 m (measured over
# 5,000 cells); 220 m keeps the per-cell distance bound a true lower bound.
CELL_RADIUS_M = 220.0


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
    """Up to `need` fresh available drivers nearest to (lat, lng), searching H3 rings 0..kmax.

    One round trip: ring_search.lua visits cells best-first by a lower bound on their distance and stops once
    no remaining cell can beat the need-th driver found (exact within the rings; it replaced the k+1 ring
    guard, which bench/micro.py showed touched ~1,000 drivers per query at Manhattan density).
    `guard=False` disables the bound check, so a test can show what goes wrong without it.
    """
    now = now if now is not None else now_ms()
    lat, lng = float(lat), float(lng)
    origin = h3.latlng_to_cell(lat, lng, config.RES_DRIVER)
    cells = [c for k in range(kmax + 1) for c in h3.grid_ring(origin, k)]
    bounded = sorted((max(0.0, haversine_m(lat, lng, *h3.cell_to_latlng(c)) - CELL_RADIUS_M), c) for c in cells)
    flat = _ring_script(r)(args=[need, now - max_age_ms, int(guard), lat, lng, len(bounded),
                                 *(v for b, c in bounded for v in (c, math.floor(b * 10) / 10))])   # round DOWN: stays a lower bound
    return [(flat[i], float(flat[i + 1])) for i in range(0, len(flat), 2)]


def _ring_script(r: redis.Redis):
    script = getattr(r, "_gridroute_ring_search", None)
    if script is None:
        script = r.register_script((LUA_DIR / "ring_search.lua").read_text())
        r._gridroute_ring_search = script
    return script


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
