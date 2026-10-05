"""Precompute OSRM route time and distance for fixed train / validate / test samples (spec §5.4).

    python -m ml.precompute                    # ~30 min at ~1,000 trips/s against the local OSRM

The SAME OSRM the matcher calls online (spec arrow 22): training and serving see the same road model.
Samples are fixed by seed so every experiment trains and tests on identical rows.
Writes $GRIDROUTE_DATA_DIR/features/{train,validate,test}.parquet: raw dispatch-time inputs + osrm_s,
osrm_m + the target (duration_s). Feature columns are built from these by core/features.py.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

import duckdb
import numpy as np

from core import config
from core.osrm import pair_routes

SPLITS = {"train": (["2016-01", "2016-02", "2016-03", "2016-04"], 1_200_000),
          "validate": (["2016-05"], 200_000),
          "test": (["2016-06"], 300_000)}
COLUMNS = ["pickup_ts", "pickup_lat", "pickup_lng", "dropoff_lat", "dropoff_lng", "duration_s", "dist_m"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    ap.add_argument("--splits", nargs="*", default=list(SPLITS))
    ap.add_argument("--scale", type=float, default=1.0, help="multiply sample sizes (e.g. 0.01 for a smoke run)")
    ap.add_argument("--seed", type=int, default=2016)
    a = ap.parse_args()
    out_dir = a.data_dir / "features"
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for split in a.splits:
        months, n = SPLITS[split]
        n = int(n * a.scale)
        files = [(a.data_dir / "clean" / "trips" / f"{m}.parquet").as_posix() for m in months]
        t0 = time.monotonic()
        rows = con.execute(f"SELECT {', '.join(COLUMNS)} FROM read_parquet({files!r}) "
                           f"USING SAMPLE {n} ROWS (reservoir, {a.seed})").fetchnumpy()
        pairs = list(zip(rows["pickup_lat"].tolist(), rows["pickup_lng"].tolist(),
                         rows["dropoff_lat"].tolist(), rows["dropoff_lng"].tolist()))
        routed = asyncio.run(pair_routes(pairs))
        osrm_s = np.array([np.nan if r is None else r[0] for r in routed])
        osrm_m = np.array([np.nan if r is None else r[1] for r in routed])
        con.register("s", {**{c: rows[c] for c in COLUMNS}, "osrm_s": osrm_s, "osrm_m": osrm_m})
        out = out_dir / f"{split}.parquet"
        con.execute(f"COPY (SELECT * FROM s ORDER BY pickup_ts) TO '{out.as_posix()}' (FORMAT parquet)")
        con.unregister("s")
        print(f"{split}: {n:,} trips from {', '.join(months)}, {np.isfinite(osrm_s).sum():,} routed, "
              f"{time.monotonic() - t0:.0f}s -> {out}", flush=True)


if __name__ == "__main__":
    main()
