"""Lua loader + typed wrappers. All multi-key writes go through here (spec §2.3 rule 2)."""
from __future__ import annotations

from enum import IntEnum
from pathlib import Path

import redis

from core import config

LUA_DIR = Path(__file__).parent / "lua"


def driver_key(driver_id: str) -> str:
    return f"driver:{driver_id}"


def lock_key(driver_id: str) -> str:
    return f"lock:driver:{driver_id}"


def ride_key(ride_id: str) -> str:
    return f"ride:{ride_id}"


def cell_key(cell: str) -> str:
    return f"cell:{cell}"


class Claim(IntEnum):
    CLAIMED = 1
    DRIVER_TAKEN = 0      # driver busy, stale, or lost the race: try the next candidate
    RIDE_HANDLED = -1     # ride is no longer queued: only re-publish


class Scripts:
    """Registers the scripts once; redis-py runs them by EVALSHA and reloads on NOSCRIPT."""

    def __init__(self, r: redis.Redis):
        self.r = r
        self._move = r.register_script((LUA_DIR / "move_driver.lua").read_text())
        self._claim = r.register_script((LUA_DIR / "claim_driver.lua").read_text())
        self._release = r.register_script((LUA_DIR / "release_driver.lua").read_text())
        self._reap = r.register_script((LUA_DIR / "reap_cells.lua").read_text())
        self._unmatch = r.register_script((LUA_DIR / "unmatch_ride.lua").read_text())

    def move_driver(self, driver_id: str, cell: str, lat: float, lng: float, seq: int, ts_ms: int) -> bool:
        """Apply a ping. False means it was stale/out-of-order and ignored."""
        res = self._move(
            keys=[driver_key(driver_id), lock_key(driver_id)],
            args=[driver_id, cell, lat, lng, seq, ts_ms],
        )
        return res == 1

    def move_drivers(self, pings: list[tuple[str, str, float, float, int, int]]) -> list[bool]:
        """Pipelined move_driver for (driver_id, cell, lat, lng, seq, ts_ms) tuples. Each call is still atomic."""
        pipe = self.r.pipeline(transaction=False)
        for driver_id, cell, lat, lng, seq, ts_ms in pings:
            self._move(
                keys=[driver_key(driver_id), lock_key(driver_id)],
                args=[driver_id, cell, lat, lng, seq, ts_ms],
                client=pipe,
            )
        return [res == 1 for res in pipe.execute()]

    def claim_driver(
        self,
        driver_id: str,
        ride_id: str,
        now_ms: int,
        lease_ms: int = config.OFFER_LEASE_MS,
        max_age_ms: int = config.MAX_AGE_MS,
    ) -> Claim:
        res = self._claim(
            keys=[driver_key(driver_id), lock_key(driver_id), ride_key(ride_id)],
            args=[driver_id, ride_id, lease_ms, now_ms, max_age_ms],
        )
        return Claim(int(res))

    def release_driver(self, driver_id: str, ride_id: str, now_ms: int) -> bool:
        """False means ride_id does not own this driver's lock (wrong or expired ride)."""
        res = self._release(
            keys=[driver_key(driver_id), lock_key(driver_id), ride_key(ride_id)],
            args=[driver_id, ride_id, now_ms],
        )
        return res == 1

    def unmatch_ride(self, ride_id: str, now_ms: int) -> bool:
        return self._unmatch(keys=[ride_key(ride_id)], args=[now_ms]) == 1

    def reap_cells(self, cells: list[str], cutoff_ms: int) -> int:
        """Evict drivers last seen before cutoff_ms from these cells. Returns how many were evicted."""
        if not cells:
            return 0
        return int(self._reap(keys=["active_cells"], args=[cutoff_ms, *cells]))


class AsyncScripts:
    """The scripts the gateway needs, for a redis.asyncio client."""

    def __init__(self, r):
        self._release = r.register_script((LUA_DIR / "release_driver.lua").read_text())

    async def release_driver(self, driver_id: str, ride_id: str, now_ms: int) -> bool:
        res = await self._release(
            keys=[driver_key(driver_id), lock_key(driver_id), ride_key(ride_id)],
            args=[driver_id, ride_id, now_ms],
        )
        return res == 1
