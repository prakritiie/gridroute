"""Central tunables (spec §10). Every value can be overridden by an env var of the same name."""
import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
OSRM_URL = os.environ.get("OSRM_URL", "http://localhost:5001")

# Trip data and derived Parquet (GBs). Point outside synced folders like OneDrive.
DATA_DIR = Path(os.environ.get("GRIDROUTE_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))

# NYC bounding box (spec §5.2 rule 1): pings and trips outside it are rejected
LAT_MIN, LAT_MAX = 40.47, 40.92
LNG_MIN, LNG_MAX = -74.27, -73.68

# H3 resolutions: indexing / ETA features + congestion / demand forecasting
RES_DRIVER = 9
RES_ETA = 8
RES_DEMAND = 7

# Freshness and leases (ms)
MAX_AGE_MS = _int("MAX_AGE_MS", 30_000)            # silent longer than this = not matchable
OFFER_LEASE_MS = _int("OFFER_LEASE_MS", 15_000)
OFFERS = bool(_int("OFFERS", 1))   # M2: matcher offers, driver must accept within OFFER_LEASE_MS
# Spec says 15 min, but a trip longer than its lease would be revived mid-ride by move_driver.
# M1 claims with this directly (no accept step yet), so it must exceed any simulated pickup + trip.
TRIP_LEASE_MS = _int("TRIP_LEASE_MS", 60 * 60_000)
DRIVER_TTL_MS = 3_600_000                          # mirrored in move_driver.lua

# Matcher / search
RING_KMAX = _int("RING_KMAX", 3)
CANDIDATES_NEED = _int("CANDIDATES_NEED", 5)
MATCH_INTERVAL_S = 2.0
MATCH_BATCH = _int("MATCH_BATCH", 500)
MAX_ATTEMPTS = _int("MAX_ATTEMPTS", 5)    # default; a replay overrides it per run via config:max_attempts
OSRM_TIMEOUT_S = 0.150

# Background workers
REAPER_INTERVAL_S = 5.0
AGGREGATOR_INTERVAL_S = 0.5
