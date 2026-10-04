"""Clean the raw 2016 trips month by month (spec §5.2-5.4).

    python -m data.clean                     # all downloaded months
    python -m data.clean --months 2016-06

Reads   $GRIDROUTE_DATA_DIR/raw/trips/YYYY-MM/*.parquet   (from data.download)
Writes  $GRIDROUTE_DATA_DIR/clean/trips/YYYY-MM.parquet   sorted by pickup time, + duration_s, dist_m
        $GRIDROUTE_DATA_DIR/clean/manifest.json           rows removed per rule per month, split, SHA-256

Splits are by time, never random (spec §5.3): Jan-Apr train, May validate, Jun test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import duckdb

from core import config

SQL_FILE = Path(__file__).with_name("clean.sql")
SPLITS = {"2016-01": "train", "2016-02": "train", "2016-03": "train", "2016-04": "train",
          "2016-05": "validate", "2016-06": "test"}

HAVERSINE_M = """2 * 6371000 * asin(sqrt(
    pow(sin(radians(dropoff_lat - pickup_lat) / 2), 2)
  + cos(radians(pickup_lat)) * cos(radians(dropoff_lat)) * pow(sin(radians(dropoff_lng - pickup_lng) / 2), 2)))"""


def load_rules(path: Path = SQL_FILE) -> list[tuple[str, str]]:
    """[(rule_name, sql)] in file order, from '-- rule: name' blocks."""
    rules, name, lines = [], None, []
    for line in path.read_text().splitlines():
        if m := re.match(r"--\s*rule:\s*(\w+)", line):
            if name:
                rules.append((name, "\n".join(lines)))
            name, lines = m.group(1), []
        elif name and not line.lstrip().startswith("--"):
            lines.append(line)
    if name:
        rules.append((name, "\n".join(lines)))
    return rules


def clean_month(con: duckdb.DuckDBPyConnection, src_glob: str, out: Path) -> dict:
    con.execute(f"CREATE OR REPLACE TEMP TABLE t AS SELECT * FROM read_parquet('{src_glob}')")
    count = lambda: con.execute("SELECT count(*) FROM t").fetchone()[0]  # noqa: E731
    raw = before = count()
    removed = {}
    for name, sql in load_rules():
        con.execute(sql)
        after = count()
        removed[name] = before - after
        before = after

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.part")
    con.execute(f"""
        COPY (
            SELECT pickup_ts, dropoff_ts, pickup_lat, pickup_lng, dropoff_lat, dropoff_lng, trip_distance_mi,
                   date_diff('second', pickup_ts, dropoff_ts)::INTEGER AS duration_s,
                   round({HAVERSINE_M})::INTEGER AS dist_m
            FROM t ORDER BY pickup_ts
        ) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 250000)
    """)
    os.replace(tmp, out)
    return {"raw_rows": raw, "removed": removed, "clean_rows": before,
            "kept_pct": round(100 * before / raw, 2) if raw else 0.0}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    ap.add_argument("--months", nargs="*", default=sorted(SPLITS))
    a = ap.parse_args()

    raw_dir, clean_dir = a.data_dir / "raw" / "trips", a.data_dir / "clean"
    manifest_path = clean_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {
        "source": "raw/manifest.json (NYC Open Data uacg-pexx, 2016 yellow taxi)",
        "rules": [name for name, _ in load_rules()],
        "splits": {"train": "2016-01..2016-04", "validate": "2016-05", "test": "2016-06"},
        "months": {},
    }
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{os.environ.get('DUCKDB_MEMORY', '4GB')}'")
    for month in a.months:
        days = sorted((raw_dir / month).glob("*.parquet"))
        if not days:
            print(f"{month}: no raw data, skipped", flush=True)
            continue
        t0 = time.monotonic()
        out = clean_dir / "trips" / f"{month}.parquet"
        stats = clean_month(con, (raw_dir / month).as_posix() + "/*.parquet", out)
        stats.update(split=SPLITS.get(month, "unused"), days=len(days), file=out.relative_to(a.data_dir).as_posix(),
                     sha256=hashlib.sha256(out.read_bytes()).hexdigest(), bytes=out.stat().st_size,
                     cleaned_at=datetime.now().isoformat(timespec="seconds"))
        manifest["months"][month] = stats
        removed = "  ".join(f"{k}={v:,}" for k, v in stats["removed"].items())
        print(f"{month} [{stats['split']}] {stats['raw_rows']:,} -> {stats['clean_rows']:,} "
              f"({stats['kept_pct']}% kept, {time.monotonic() - t0:.0f}s)\n    removed: {removed}", flush=True)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
