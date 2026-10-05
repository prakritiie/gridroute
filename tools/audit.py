"""Invariant audit (spec §14). Run after every load test / replay, or live; exits non-zero on violations.

    python -m tools.audit [--redis-url URL] [--max-age-ms N]

Checks
  C1  each driver holds at most one ride, each ride has at most one driver,
      and for every assigned driver lock:driver:{id} == driver.ride and ride.driver == driver
  C2  every fresh available driver is in exactly one cell (its own); assigned drivers are in none

Safe on a live system: the first pass reads in bulk (fast, not atomic), so a driver that moves or gets
claimed between reading its hash and reading the cells can look wrong. Every flagged driver is then
re-checked from one atomic Lua snapshot, and only violations that survive are reported.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict

import redis

from core import config

OPEN = ("offered", "assigned")      # ride states that hold a driver

# Atomic read of everything one driver's invariants depend on.
# KEYS[1] = driver:{id}, KEYS[2] = lock:driver:{id}, KEYS[3..] = cell keys to test membership in
# ARGV[1] = driver_id.
# Returns {status, cell, ts, ride, lock, ride.driver, ride.status, in_own_current_cell, in_cell_1, ...}
# The driver's CURRENT cell is read and tested inside the script: it may have moved since pass 1.
SNAPSHOT_LUA = """
local h = redis.call('HMGET', KEYS[1], 'status', 'cell', 'ts', 'ride')
local lock = redis.call('GET', KEYS[2])
local rd, rs = false, false
if h[4] then
  rd = redis.call('HGET', 'ride:' .. h[4], 'driver')
  rs = redis.call('HGET', 'ride:' .. h[4], 'status')
end
local own = h[2] and redis.call('ZSCORE', 'cell:' .. h[2], ARGV[1]) and '1' or '0'
local out = {h[1] or '', h[2] or '', h[3] or '0', h[4] or '', lock or '', rd or '', rs or '', own}
for i = 3, #KEYS do
  out[#out + 1] = redis.call('ZSCORE', KEYS[i], ARGV[1]) and '1' or '0'
end
return out
"""

# KEYS = ride keys. Returns {status, driver} pairs flattened.
RIDES_LUA = """
local out = {}
for i = 1, #KEYS do
  local v = redis.call('HMGET', KEYS[i], 'status', 'driver')
  out[#out + 1] = v[1] or ''
  out[#out + 1] = v[2] or ''
end
return out
"""


def _check_driver(d: str, h: dict, cells: list[str], lock: str | None, ride_driver: str | None,
                  now_ms: int, max_age_ms: int) -> list[str]:
    errors = []
    status = h.get("status")
    if status == "assigned":
        ride = h.get("ride")
        if not ride:
            errors.append(f"C1 driver {d} assigned without a ride")
        else:
            # A missing lock is a legal expired lease (healed on next ping); a *different* owner is not.
            if lock and lock != ride:
                errors.append(f"C1 driver {d} ride={ride} but lock owned by {lock}")
            if ride_driver != d:
                errors.append(f"C1 driver {d} holds ride {ride} but ride.driver={ride_driver}")
        if cells:
            errors.append(f"C2 assigned driver {d} still searchable in {cells}")
    elif status == "available" and now_ms - int(h.get("ts") or 0) <= max_age_ms:
        if cells != [h.get("cell")]:
            errors.append(f"C2 driver {d} should be only in {h.get('cell')}, found in {cells}")
    elif len(cells) > 1:
        errors.append(f"C2 driver {d} in multiple cells {cells}")
    return errors


def _scan_hashes(r: redis.Redis, pattern: str) -> dict[str, dict[str, str]]:
    keys = list(r.scan_iter(match=pattern, count=1000, _type="HASH"))
    pipe = r.pipeline(transaction=False)
    for k in keys:
        pipe.hgetall(k)
    return dict(zip(keys, pipe.execute()))


def audit(r: redis.Redis, now_ms: int | None = None, max_age_ms: int = config.MAX_AGE_MS,
          recheck: bool = True) -> list[str]:
    """Return human-readable violations (empty = healthy). Expects decode_responses=True.

    Pass now_ms only for tests with synthetic timestamps; live audits use the wall clock in both passes.
    """
    live = now_ms is None
    now_ms = int(time.time() * 1000) if live else now_ms

    # ---- pass 1: bulk, non-atomic --------------------------------------------
    drivers = {k.split(":", 1)[1]: h for k, h in _scan_hashes(r, "driver:*").items()}

    membership: dict[str, list[str]] = defaultdict(list)
    cell_keys = list(r.scan_iter(match="cell:*", count=1000, _type="ZSET"))
    pipe = r.pipeline(transaction=False)
    for ck in cell_keys:
        pipe.zrange(ck, 0, -1)
    for ck, members in zip(cell_keys, pipe.execute()):
        for d in members:
            membership[d].append(ck.split(":", 1)[1])

    assigned = [d for d, h in drivers.items() if h.get("status") == "assigned" and h.get("ride")]
    pipe = r.pipeline(transaction=False)
    for d in assigned:
        pipe.get(f"lock:driver:{d}")
        pipe.hget(f"ride:{drivers[d]['ride']}", "driver")
    vals = pipe.execute()
    lock_of = {d: vals[2 * i] for i, d in enumerate(assigned)}
    ride_driver_of = {d: vals[2 * i + 1] for i, d in enumerate(assigned)}

    flagged: dict[str, list[str]] = {}
    for d, h in drivers.items():
        errs = _check_driver(d, h, sorted(membership.get(d, [])), lock_of.get(d), ride_driver_of.get(d),
                             now_ms, max_age_ms)
        if errs:
            flagged[d] = errs

    # Two drivers pointing at the same ride
    drivers_per_ride: dict[str, list[str]] = defaultdict(list)
    for d in assigned:
        drivers_per_ride[drivers[d]["ride"]].append(d)
    shared_rides = {ride: ds for ride, ds in drivers_per_ride.items() if len(ds) > 1}

    # One driver named by two open rides
    rides_per_driver: dict[str, list[str]] = defaultdict(list)
    for k, h in _scan_hashes(r, "ride:*").items():
        if h.get("status") in OPEN and h.get("driver"):
            rides_per_driver[h["driver"]].append(k.split(":", 1)[1])
    double_booked = {d: rides for d, rides in rides_per_driver.items() if len(rides) > 1}

    orphans = {d: membership[d] for d in membership.keys() - drivers.keys()}

    if not recheck:
        errors = [e for errs in flagged.values() for e in errs]
        errors += [f"C1 ride {ride} booked to {len(ds)} drivers: {sorted(ds)}" for ride, ds in shared_rides.items()]
        errors += [f"C1 driver {d} double-booked on rides {sorted(x)}" for d, x in double_booked.items()]
        errors += [f"C2 cell entry for unknown driver {d} in {c}" for d, c in orphans.items()]
        return errors

    # ---- pass 2: atomic recheck of everything flagged --------------------------
    snap = r.register_script(SNAPSHOT_LUA)
    rides_snap = r.register_script(RIDES_LUA)
    errors: list[str] = []

    for d in set(flagged) | {d for ds in shared_rides.values() for d in ds} | set(orphans):
        h = drivers.get(d, {})
        check_cells = sorted(set(membership.get(d, [])) | ({h["cell"]} if h.get("cell") else set()))
        out = snap(keys=[f"driver:{d}", f"lock:driver:{d}", *[f"cell:{c}" for c in check_cells]], args=[d])
        status, cell, ts, ride, lock, ride_driver, _ride_status, in_own = out[:8]
        in_cells = {c for c, flag in zip(check_cells, out[8:]) if flag == "1"}
        if in_own == "1":
            in_cells.add(cell)
        in_cells = sorted(in_cells)
        if not status:                                   # hash expired/absent
            if in_cells:
                errors.append(f"C2 cell entry for unknown driver {d} in {in_cells}")
            continue
        errors += _check_driver(d, {"status": status, "cell": cell, "ts": ts, "ride": ride}, in_cells,
                                lock or None, ride_driver or None,
                                int(time.time() * 1000) if live else now_ms, max_age_ms)

    for ride, ds in shared_rides.items():
        out = [snap(keys=[f"driver:{d}", f"lock:driver:{d}"], args=[d]) for d in ds]
        still = [d for d, o in zip(ds, out) if o[0] == "assigned" and o[3] == ride]
        if len(still) > 1:
            errors.append(f"C1 ride {ride} booked to {len(still)} drivers: {sorted(still)}")

    for d, rides in double_booked.items():
        out = rides_snap(keys=[f"ride:{x}" for x in rides])
        still = [x for i, x in enumerate(rides) if out[2 * i] in OPEN and out[2 * i + 1] == d]
        if len(still) > 1:
            errors.append(f"C1 driver {d} double-booked on rides {sorted(still)}")

    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    ap.add_argument("--max-age-ms", type=int, default=config.MAX_AGE_MS)
    ap.add_argument("--no-recheck", action="store_true", help="report raw first-pass findings")
    a = ap.parse_args()
    r = redis.Redis.from_url(a.redis_url, decode_responses=True)
    errors = audit(r, max_age_ms=a.max_age_ms, recheck=not a.no_recheck)
    for e in errors:
        print(e)
    print(f"audit: {'FAIL' if errors else 'OK'} ({len(errors)} violations)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
