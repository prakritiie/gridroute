"""OSRM client shared by the simulator, the congestion builder and (M3) the feature builder and matcher.

pair_durations: many independent A->B trips. One /table call per batch of 50 trips (50 sources x 50
    destinations, keep the diagonal) is ~4x faster than 50 /route calls: measured 1,070 vs 250 trips/s.
route: one path with its geometry, for simulated drivers that follow real streets.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

import httpx

from core import config

TABLE_BATCH = 50


@dataclass
class Route:
    duration_s: float              # free-flow OSRM time
    distance_m: float
    coords: list[tuple[float, float]]   # (lat, lng) along the road
    cum: list[float]               # metres from the start to each coord


def _coord(lat: float, lng: float) -> str:
    return f"{lng:.6f},{lat:.6f}"


async def pair_routes(
    pairs: list[tuple[float, float, float, float]],
    base_url: str = config.OSRM_URL,
    batch: int = TABLE_BATCH,
    concurrency: int = 8,
) -> list[tuple[float, float] | None]:
    """Free-flow (seconds, metres) for each (from_lat, from_lng, to_lat, to_lng); None where OSRM finds no route."""
    sem = asyncio.Semaphore(concurrency)
    out: list[tuple[float, float] | None] = [None] * len(pairs)
    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        async def run(start: int) -> None:
            chunk = pairs[start:start + batch]
            k = len(chunk)
            coords = ";".join(_coord(p[0], p[1]) for p in chunk) + ";" + ";".join(_coord(p[2], p[3]) for p in chunk)
            params = {"sources": ";".join(map(str, range(k))), "destinations": ";".join(map(str, range(k, 2 * k))),
                      "annotations": "duration,distance"}
            j = None
            async with sem:
                for attempt in range(3):
                    try:
                        j = (await client.get(f"/table/v1/driving/{coords}", params=params)).json()
                        break
                    except (httpx.HTTPError, ValueError):
                        if attempt == 2:
                            return
                        await asyncio.sleep(0.5 * (attempt + 1))
            if not j or j.get("code") != "Ok":
                return
            for i in range(k):
                d, m = j["durations"][i][i], j["distances"][i][i]
                out[start + i] = (float(d), float(m)) if d is not None and m is not None else None

        await asyncio.gather(*(run(s) for s in range(0, len(pairs), batch)))
    return out


async def pair_durations(pairs, base_url: str = config.OSRM_URL, **kw) -> list[float | None]:
    """Free-flow seconds only (see pair_routes)."""
    return [None if r is None else r[0] for r in await pair_routes(pairs, base_url, **kw)]


def _hav_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


async def route(client: httpx.AsyncClient, frm: tuple[float, float], to: tuple[float, float]) -> Route | None:
    """Road path from frm to to, as (lat, lng) points with cumulative distance."""
    try:
        j = (await client.get(f"/route/v1/driving/{_coord(*frm)};{_coord(*to)}",
                              params={"overview": "full", "geometries": "geojson", "steps": "false"})).json()
    except (httpx.HTTPError, ValueError):
        return None
    if j.get("code") != "Ok" or not j.get("routes"):
        return None
    r = j["routes"][0]
    coords = [(lat, lng) for lng, lat in r["geometry"]["coordinates"]] or [frm, to]
    cum = [0.0]
    for a, b in zip(coords, coords[1:]):
        cum.append(cum[-1] + _hav_m(a, b))
    return Route(float(r["duration"]), float(r["distance"]), coords, cum)


_sync_client: httpx.Client | None = None


def table(sources: list[tuple[float, float]], destinations: list[tuple[float, float]],
          base_url: str = config.OSRM_URL, timeout_s: float = config.OSRM_TIMEOUT_S):
    """One many-to-many call for the matcher: (durations, distances) as [source][destination] lists of
    seconds / metres (None where unroutable), or None if OSRM doesn't answer within timeout_s.
    The caller falls back to B3 on None, so dispatch never waits on routing."""
    global _sync_client
    if _sync_client is None:
        _sync_client = httpx.Client(base_url=base_url, timeout=timeout_s)
    coords = ";".join(_coord(*p) for p in sources) + ";" + ";".join(_coord(*p) for p in destinations)
    n = len(sources)
    params = {"sources": ";".join(map(str, range(n))),
              "destinations": ";".join(map(str, range(n, n + len(destinations)))),
              "annotations": "duration,distance"}
    try:
        j = _sync_client.get(f"/table/v1/driving/{coords}", params=params, timeout=timeout_s).json()
    except (httpx.HTTPError, ValueError):
        return None
    if j.get("code") != "Ok":
        return None
    return j["durations"], j["distances"]
