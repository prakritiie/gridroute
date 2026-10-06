"""Recorder: one run's events -> runs/{run_id}/ for the evaluation report (spec arrows 15, 28).

Stream IDs are millisecond timestamps, so a run's events are exactly XRANGE stream:events start..end:
no consumer group or run tagging needed, and re-recording a run is idempotent.

    runs/{run_id}/events.parquet   every event in the run (match, batch, pickup, dropoff, unmatched, ...)
    runs/{run_id}/rides.parquet    final state of every ride the replay requested, plus its real facts
    runs/{run_id}/run.json         the run configuration
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import duckdb
import redis

EVENT_COLUMNS = ["id", "type", "ride", "driver", "t", "sim_t", "leg_s", "leg_m", "osrm_s", "wait_s", "match_s",
                 "trip_s", "trip_m", "dist_m", "eta_s", "attempts", "ms", "n",
                 "drivers", "sim_hours", "empty_km", "loaded_km", "conflicts", "repos_km", "repositions"]
NUMERIC = set(EVENT_COLUMNS) - {"id", "type", "ride", "driver"}


def _events(r: redis.Redis, start_id: str, end_id: str) -> list[dict]:
    out, cursor = [], start_id
    while True:
        batch = r.xrange("stream:events", min=cursor, max=end_id, count=10_000)
        if cursor != start_id:
            batch = batch[1:] if batch and batch[0][0] == cursor else batch
        if not batch:
            return out
        out += [{"id": i, **f} for i, f in batch]
        cursor = batch[-1][0]


def _write_parquet(con, path: Path, columns: list[str], types: dict[str, str], rows) -> None:
    tmp = path.with_suffix(".csv")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for row in rows:
            w.writerow(["" if v is None else v for v in row])
    con.execute(f"""COPY (SELECT * FROM read_csv('{tmp.as_posix()}', header=true, nullstr='', columns={types!r}))
                    TO '{path.as_posix()}' (FORMAT parquet)""")
    tmp.unlink()


def record(r: redis.Redis, run_id: str, start_id: str, end_id: str, rides: dict[str, dict], cfg: dict,
           runs_dir: Path) -> Path:
    out = runs_dir / run_id
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()

    # Bulk path: CSV -> one typed read_csv. (DuckDB's executemany inserts row by row: 35k events took >10 min.)
    events = _events(r, start_id, end_id)
    _write_parquet(con, out / "events.parquet", EVENT_COLUMNS,
                   {c: ("DOUBLE" if c in NUMERIC else "VARCHAR") for c in EVENT_COLUMNS},
                   ([e.get(c) for c in EVENT_COLUMNS] for e in events))

    pipe = r.pipeline(transaction=False)
    ids = list(rides)
    for rid in ids:
        pipe.hmget(f"ride:{rid}", "status", "driver", "attempts", "eta_pred", "eta_fallback")
    states = pipe.execute()
    ride_cols = {"ride": "VARCHAR", "status": "VARCHAR", "driver": "VARCHAR", "attempts": "INTEGER",
                 "req_sim": "DOUBLE", "assigned_sim": "DOUBLE", "real_trip_s": "DOUBLE",
                 "eta_pred": "DOUBLE", "eta_fallback": "INTEGER"}
    _write_parquet(con, out / "rides.parquet", list(ride_cols), ride_cols, (
        [rid, s[0], s[1], int(s[2]) if s[2] else 0, rides[rid]["req_sim"], rides[rid].get("assigned_sim"),
         float(rides[rid]["real_s"]), float(s[3]) if s[3] else None, int(s[4]) if s[4] else 0]
        for rid, s in zip(ids, states)))
    (out / "run.json").write_text(json.dumps({"run_id": run_id, **cfg, "events": len(events), "rides": len(ids)}, indent=2))
    return out
