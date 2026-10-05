"""Three ways to answer "the 5 nearest fresh drivers within ~1 km", behind one interface (spec C5, arrow 32).

  h3       GridRoute's index: H3 res-9 cell sorted sets, ring search with the k+1 guard (core/search.py,
           the matcher's own code); writes through move_driver.lua (seq check, status, cell move, freshness)
  geo      Redis GEOADD / GEOSEARCH BYRADIUS ASC COUNT: one sorted set keyed by geohash
  postgis  PostgreSQL + PostGIS: geography column, GiST index, ST_DWithin + KNN ORDER BY <->

Same data, same question, same radius for all three. Each uses its own Redis DB / database so nothing
collides with the live system.
"""
from __future__ import annotations

import os
import time
from typing import Protocol

import h3
import psycopg
import redis

from core import config
from core.scripts import Scripts
from core.search import candidates

RADIUS_M = 1000        # ~ what a 3-ring res-9 search covers (+ the guard ring)
NEED = 5
# Both freshness-aware backends keep their freshness predicate (so its cost is measured), with the window
# widened so a dataset loaded once stays valid for a whole run. Redis GEO has no notion of freshness at all.
FRESH_MS = 24 * 3600 * 1000
REDIS_BASE = os.environ.get("BENCH_REDIS_URL", "redis://localhost:6379")
PG_DSN = os.environ.get("BENCH_PG_DSN", "postgresql://bench:bench@localhost:5433/bench")


class Backend(Protocol):
    name: str

    def reset(self) -> None: ...
    def load(self, drivers: list[tuple[str, float, float]]) -> None: ...
    def update(self, moves: list[tuple[str, float, float, int]]) -> None: ...
    def near(self, lat: float, lng: float) -> list[str]: ...


class H3Backend:
    name = "h3"

    def __init__(self, url: str = REDIS_BASE + "/2"):
        self.r = redis.Redis.from_url(url, decode_responses=True)
        self.s = Scripts(self.r)

    def reset(self) -> None:
        self.r.flushdb()

    def load(self, drivers) -> None:
        now = int(time.time() * 1000)
        for i in range(0, len(drivers), 5000):
            self.s.move_drivers([(d, h3.latlng_to_cell(la, ln, config.RES_DRIVER), la, ln, 1, now)
                                 for d, la, ln in drivers[i:i + 5000]])

    def update(self, moves) -> None:
        now = int(time.time() * 1000)
        self.s.move_drivers([(d, h3.latlng_to_cell(la, ln, config.RES_DRIVER), la, ln, seq, now)
                             for d, la, ln, seq in moves])

    def near(self, lat, lng) -> list[str]:
        # Same question as the others: the 5 nearest within RADIUS_M (rings can reach a little further)
        return [d for d, dist in candidates(self.r, lat, lng, need=NEED, max_age_ms=FRESH_MS) if dist <= RADIUS_M]


class GeoBackend:
    name = "geo"
    KEY = "drivers:geo"

    def __init__(self, url: str = REDIS_BASE + "/3"):
        self.r = redis.Redis.from_url(url, decode_responses=True)

    def reset(self) -> None:
        self.r.flushdb()

    def load(self, drivers) -> None:
        for i in range(0, len(drivers), 5000):
            vals = []
            for d, la, ln in drivers[i:i + 5000]:
                vals += [ln, la, d]
            self.r.geoadd(self.KEY, vals)

    def update(self, moves) -> None:
        vals = []
        for d, la, ln, _seq in moves:
            vals += [ln, la, d]
        self.r.geoadd(self.KEY, vals)

    def near(self, lat, lng) -> list[str]:
        return self.r.geosearch(self.KEY, longitude=lng, latitude=lat, radius=RADIUS_M, unit="m",
                                sort="ASC", count=NEED)


class PostgisBackend:
    name = "postgis"

    def __init__(self, dsn: str = PG_DSN):
        self.dsn = dsn
        self._conn = None

    @property
    def conn(self):
        # Lazy, with retries: PostGIS's first boot restarts the server once after initdb
        for attempt in range(30):
            if self._conn is not None and not self._conn.closed:
                return self._conn
            try:
                self._conn = psycopg.connect(self.dsn, autocommit=True)
            except psycopg.OperationalError:
                if attempt == 29:
                    raise
                time.sleep(1)
        return self._conn

    def reset(self) -> None:
        with self.conn.cursor() as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            c.execute("DROP TABLE IF EXISTS drivers")
            c.execute("""CREATE TABLE drivers (id text PRIMARY KEY, geog geography(Point, 4326) NOT NULL,
                                               ts timestamptz NOT NULL DEFAULT now())""")
            c.execute("CREATE INDEX drivers_geog ON drivers USING gist (geog)")

    def load(self, drivers) -> None:
        with self.conn.cursor() as c:
            with c.copy("COPY drivers (id, geog) FROM STDIN") as cp:
                for d, la, ln in drivers:
                    cp.write_row((d, f"SRID=4326;POINT({ln} {la})"))
            c.execute("ANALYZE drivers")

    def update(self, moves) -> None:
        # The idiomatic bulk path (COPY into a temp table, one UPDATE ... FROM), not row-at-a-time UPDATEs
        with self.conn.cursor() as c:
            c.execute("CREATE TEMP TABLE IF NOT EXISTS mv (id text, lng float8, lat float8)")
            c.execute("TRUNCATE mv")
            with c.copy("COPY mv FROM STDIN") as cp:
                for d, la, ln, _seq in moves:
                    cp.write_row((d, ln, la))
            c.execute("""UPDATE drivers SET geog = ST_SetSRID(ST_MakePoint(mv.lng, mv.lat), 4326)::geography, ts = now()
                         FROM mv WHERE drivers.id = mv.id""")

    def near(self, lat, lng) -> list[str]:
        with self.conn.cursor() as c:
            c.execute("""SELECT id FROM drivers
                         WHERE ST_DWithin(geog, ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography, %(r)s)
                           AND ts > now() - interval '1 day'
                         ORDER BY geog <-> ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography
                         LIMIT %(n)s""", {"lat": lat, "lng": lng, "r": RADIUS_M, "n": NEED})
            return [row[0] for row in c.fetchall()]


BACKENDS = {"h3": H3Backend, "geo": GeoBackend, "postgis": PostgisBackend}
