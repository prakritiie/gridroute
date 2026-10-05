"""Real NYC pickup / drop-off locations for the live demo, so simulated cars and riders stand where real
taxis did (streets, not rivers).

    python -m sim.points          # regenerates sim/data/nyc_points.npy from the clean June 2016 trips

The committed sample (40,000 points, float32 lat/lng, ~320 KB) is derived from NYC Open Data's public
2016 Yellow Taxi Trip Data; see docs/data_card.md.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

POINTS = Path(__file__).parent / "data" / "nyc_points.npy"


def load(path: Path = POINTS) -> np.ndarray:
    return np.load(path).astype(np.float64)


def main() -> None:
    import duckdb

    from core import config
    src = (config.DATA_DIR / "clean" / "trips" / "2016-06.parquet").as_posix()
    rows = duckdb.connect().execute(f"""
        SELECT lat, lng FROM (
            SELECT pickup_lat AS lat, pickup_lng AS lng FROM '{src}'
            UNION ALL SELECT dropoff_lat, dropoff_lng FROM '{src}')
        USING SAMPLE 40000 ROWS (reservoir, 42)""").fetchnumpy()
    pts = np.column_stack([rows["lat"], rows["lng"]]).astype(np.float32)
    POINTS.parent.mkdir(exist_ok=True)
    np.save(POINTS, pts)
    print(f"wrote {len(pts):,} points -> {POINTS} ({POINTS.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
