"""Real NYC taxi trips for the live demo, so riders ask for rides real New Yorkers took and cars stand where
real taxis did (streets, not rivers).

    python -m sim.points          # regenerates sim/data/nyc_trips.npy from the clean June 2016 trips

The committed sample (30,000 trips: pickup lat/lng, drop-off lat/lng as float32, ~470 KB) is derived from
NYC Open Data's public 2016 Yellow Taxi Trip Data; see docs/data_card.md. Real pairs matter: random pairs
of real points had a 3.1 km median, real trips 2.1 km, and the longer synthetic trips quietly pushed the
5,000-car demo toward saturation.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

TRIPS = Path(__file__).parent / "data" / "nyc_trips.npy"


def load_trips(path: Path = TRIPS) -> np.ndarray:
    """(n, 4): pickup_lat, pickup_lng, dropoff_lat, dropoff_lng."""
    return np.load(path).astype(np.float64)


def load(path: Path = TRIPS) -> np.ndarray:
    """(2n, 2): every real pickup and drop-off location."""
    t = load_trips(path)
    return np.vstack([t[:, :2], t[:, 2:]])


def main() -> None:
    import duckdb

    from core import config
    src = (config.DATA_DIR / "clean" / "trips" / "2016-06.parquet").as_posix()
    rows = duckdb.connect().execute(f"""
        SELECT pickup_lat, pickup_lng, dropoff_lat, dropoff_lng FROM '{src}'
        USING SAMPLE 30000 ROWS (reservoir, 42)""").fetchnumpy()
    trips = np.column_stack([rows[c] for c in ("pickup_lat", "pickup_lng", "dropoff_lat", "dropoff_lng")])
    TRIPS.parent.mkdir(exist_ok=True)
    np.save(TRIPS, trips.astype(np.float32))
    print(f"wrote {len(trips):,} trips -> {TRIPS} ({TRIPS.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
