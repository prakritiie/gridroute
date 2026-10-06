"""Ride requests per res-7 hex per 15-minute slot, Jan-Jun 2016 (spec §8.2 target).

    python -m ml.demand_data          # -> $GRIDROUTE_DATA_DIR/features/demand_counts.npz (~1 min)

Dense: every hex that matters x every slot, zeros included (a quiet slot is information, not a gap).
Hexes kept: those with at least MIN_TRIPS pickups over the training months, so the model never sees a hex
that only appears in the test month. Slot index 0 = 2016-01-01 00:00 local NYC time.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import numpy as np

from core import config

MIN_TRIPS = 500
SLOT_S = 900
EPOCH = np.datetime64("2016-01-01T00:00:00")
N_SLOTS = 182 * 96                      # 1 Jan - 30 Jun 2016


def build(data_dir: Path) -> tuple[np.ndarray, list[str]]:
    con = duckdb.connect()
    con.execute("INSTALL h3 FROM community; LOAD h3;")
    files = [(data_dir / "clean" / "trips" / f"2016-0{m}.parquet").as_posix() for m in range(1, 7)]
    rows = con.execute(f"""
        SELECT h3_h3_to_string(h3_latlng_to_cell(pickup_lat, pickup_lng, {config.RES_DEMAND})) AS hex,
               CAST(floor(epoch(pickup_ts - TIMESTAMP '2016-01-01') / {SLOT_S}) AS INTEGER) AS slot,
               count(*) AS n
        FROM read_parquet({files!r}) GROUP BY ALL""").fetchnumpy()
    train_end = int((np.datetime64("2016-05-01") - EPOCH) / np.timedelta64(SLOT_S, "s"))
    keep = {}
    for h, s, n in zip(rows["hex"], rows["slot"], rows["n"]):
        if s < train_end:
            keep[h] = keep.get(h, 0) + int(n)
    hexes = sorted(h for h, n in keep.items() if n >= MIN_TRIPS)
    idx = {h: i for i, h in enumerate(hexes)}
    counts = np.zeros((len(hexes), N_SLOTS), dtype=np.float32)
    for h, s, n in zip(rows["hex"], rows["slot"], rows["n"]):
        if h in idx and 0 <= s < N_SLOTS:
            counts[idx[h], s] = n
    return counts, hexes


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    a = ap.parse_args()
    counts, hexes = build(a.data_dir)
    out = a.data_dir / "features" / "demand_counts.npz"
    np.savez_compressed(out, counts=counts, hexes=np.array(hexes))
    kept = counts.sum()
    print(f"{len(hexes)} hexes x {counts.shape[1]:,} slots, {kept:,.0f} requests "
          f"(median slot {np.median(counts):.0f}, busiest hex mean {counts.mean(axis=1).max():.0f}/slot) -> {out}")


if __name__ == "__main__":
    main()
