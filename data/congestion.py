"""June congestion factors: the simulator's ground-truth travel time (spec §6.3).

    python -m data.congestion --sample 500000

factor = median(actual trip duration / free-flow OSRM duration), keyed by the pickup's res-8 hex and the
hour of the week, computed from JUNE trips only. The ETA model trains on Jan-Apr and never sees June, so a
replay graded with these factors can't reward the model for memorising its own training data.
(Both still share OSRM as a base; the README states that limitation.)

Sparse keys fall back level by level, each needing >= MIN_N trips:
    (res8, hour-of-week) -> (res7, hour-of-week) -> (hour-of-week) -> global
Writes $GRIDROUTE_DATA_DIR/clean/congestion_june.parquet: level, h3, how, factor, n
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

import duckdb
import h3
import numpy as np

from core import config
from core.osrm import pair_durations

MIN_N = 20
CLIP = (0.8, 6.0)     # a factor < 0.8 means faster than free flow on the whole trip: noise, not traffic


def hour_of_week(dow_mon0: np.ndarray, hour: np.ndarray) -> np.ndarray:
    return dow_mon0 * 24 + hour


def build(con: duckdb.DuckDBPyConnection, trips: np.ndarray, osrm_s: np.ndarray) -> list[tuple]:
    """trips: structured rows (lat, lng, how, actual_s). Returns rows (level, h3, how, factor, n)."""
    ok = (osrm_s > 30) & np.isfinite(osrm_s)
    ratio = np.clip(trips["actual_s"][ok] / osrm_s[ok], *CLIP)
    lat, lng, how = trips["lat"][ok], trips["lng"][ok], trips["how"][ok]
    r8 = np.array([h3.latlng_to_cell(a, b, config.RES_ETA) for a, b in zip(lat, lng)])
    r7 = np.array([h3.cell_to_parent(c, config.RES_DEMAND) for c in r8])
    con.register("f", {"r8": r8, "r7": r7, "how": how, "ratio": ratio})
    rows = []
    for level, key in (("res8", "r8"), ("res7", "r7")):
        rows += [(level, *r) for r in con.execute(f"""
            SELECT {key}, how, median(ratio), count(*) FROM f GROUP BY ALL HAVING count(*) >= {MIN_N}""").fetchall()]
    rows += [("how", "", *r) for r in con.execute(
        f"SELECT how, median(ratio), count(*) FROM f GROUP BY how HAVING count(*) >= {MIN_N}").fetchall()]
    rows.append(("global", "", -1, float(np.median(ratio)), int(len(ratio))))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    ap.add_argument("--sample", type=int, default=500_000)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    con = duckdb.connect()
    src = (a.data_dir / "clean" / "trips" / "2016-06.parquet").as_posix()
    rows = con.execute(f"""
        SELECT pickup_lat, pickup_lng, dropoff_lat, dropoff_lng, duration_s,
               (isodow(pickup_ts) - 1) * 24 + hour(pickup_ts) AS how
        FROM '{src}' USING SAMPLE {a.sample} ROWS (reservoir, {a.seed})
    """).fetchnumpy()
    t0 = time.monotonic()
    pairs = list(zip(rows["pickup_lat"], rows["pickup_lng"], rows["dropoff_lat"], rows["dropoff_lng"]))
    osrm = np.array([np.nan if d is None else d for d in asyncio.run(pair_durations(pairs))], dtype=float)
    print(f"OSRM: {np.isfinite(osrm).sum():,}/{len(pairs):,} routed in {time.monotonic() - t0:.0f}s")

    trips = np.zeros(len(pairs), dtype=[("lat", float), ("lng", float), ("how", int), ("actual_s", float)])
    trips["lat"], trips["lng"], trips["how"], trips["actual_s"] = (
        rows["pickup_lat"], rows["pickup_lng"], rows["how"], rows["duration_s"])
    out_rows = build(con, trips, osrm)

    out = a.data_dir / "clean" / "congestion_june.parquet"
    con.execute("CREATE TABLE c (level VARCHAR, h3 VARCHAR, how INTEGER, factor DOUBLE, n INTEGER)")
    con.executemany("INSERT INTO c VALUES (?, ?, ?, ?, ?)", out_rows)
    con.execute(f"COPY c TO '{out.as_posix()}' (FORMAT parquet)")
    for level, n, med in con.execute(
            "SELECT level, count(*), median(factor) FROM c GROUP BY level ORDER BY count(*) DESC").fetchall():
        print(f"  {level:>6}: {n:,} keys, median factor {med:.2f}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
