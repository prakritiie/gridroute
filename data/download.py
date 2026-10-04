"""Download 2016 yellow-taxi trips WITH coordinates from NYC Open Data (spec §5.1, §5.4).

    python -m data.download                                  # Jan-Jun 2016, 8 workers, resumable
    python -m data.download --start 2016-06-01 --end 2016-06-07

Why this source: TLC's own Parquet files for 2016 carry only zone IDs (checked 4 Oct 2026), so the
Socrata API on dataset uacg-pexx is the only public copy with exact pickup/dropoff coordinates.

Output under GRIDROUTE_DATA_DIR (default ./data):
    raw/trips/2016-01/2016-01-12.parquet     one file per day, raw (cleaning is clean.sql's job)
    raw/manifest.json                        query, rows, source count and SHA-256 per day

Each day is fetched as 24 hourly queries (unordered full-day queries time out server-side), checked
against the API's count(*) for that day, then converted to zstd Parquet with DuckDB. Days whose
Parquet already exists are skipped, so an interrupted run just resumes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import duckdb

from core import config

DATASET = "uacg-pexx"
BASE = f"https://data.cityofnewyork.us/resource/{DATASET}"
COLUMNS = [
    "tpep_pickup_datetime", "tpep_dropoff_datetime",
    "pickup_latitude", "pickup_longitude", "dropoff_latitude", "dropoff_longitude",
    "trip_distance",
]
HOUR_LIMIT = 100_000          # busiest 2016 hour is ~35k trips; hitting this means a window was truncated
TS = "%Y-%m-%dT%H:%M:%S"

Fetch = Callable[[str], str]


def _http_get(url: str, retries: int = 6, timeout: float = 180) -> str:
    headers = {"Accept-Encoding": "identity"}
    if token := os.environ.get("SOCRATA_APP_TOKEN"):
        headers["X-App-Token"] = token
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001 - network: timeouts, 429, 5xx, resets all get the same retry
            if attempt == retries - 1:
                raise
            wait = min(120, 5 * 2 ** attempt)
            print(f"  retry {attempt + 1}/{retries - 1} in {wait}s: {type(e).__name__}: {e}", flush=True)
            time.sleep(wait)
    raise AssertionError("unreachable")


def _where(start: datetime, end: datetime) -> str:
    return f"tpep_pickup_datetime >= '{start.strftime(TS)}' AND tpep_pickup_datetime < '{end.strftime(TS)}'"


def hour_url(start: datetime) -> str:
    q = {"$select": ",".join(COLUMNS), "$where": _where(start, start + timedelta(hours=1)), "$limit": HOUR_LIMIT}
    return f"{BASE}.csv?{urllib.parse.urlencode(q)}"


def count_url(day: date) -> str:
    start = datetime(day.year, day.month, day.day)
    q = {"$select": "count(*) AS n", "$where": _where(start, start + timedelta(days=1))}
    return f"{BASE}.json?{urllib.parse.urlencode(q)}"


def day_path(root: Path, day: date) -> Path:
    return root / "raw" / "trips" / day.strftime("%Y-%m") / f"{day.isoformat()}.parquet"


def download_day(day: date, root: Path, fetch: Fetch = _http_get) -> dict:
    """Fetch one day, verify the row count, write Parquet. Returns its manifest entry."""
    out = day_path(root, day)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_csv = out.with_suffix(".csv.part")

    rows = 0
    with open(tmp_csv, "w", encoding="utf-8", newline="") as f:
        f.write(",".join(COLUMNS) + "\n")
        for h in range(24):
            text = fetch(hour_url(datetime(day.year, day.month, day.day, h)))
            lines = text.splitlines()[1:]                     # drop each response's header
            if len(lines) >= HOUR_LIMIT:
                raise RuntimeError(f"{day} {h:02d}:00 hit the {HOUR_LIMIT}-row limit; window truncated")
            rows += len(lines)
            if lines:
                f.write("\n".join(lines) + "\n")

    source_count = int(json.loads(fetch(count_url(day)))[0]["n"])
    if rows != source_count:
        tmp_csv.unlink()
        raise RuntimeError(f"{day}: downloaded {rows} rows but the API reports {source_count}")

    tmp_parquet = out.with_suffix(".parquet.part")
    con = duckdb.connect()
    con.execute(f"""
        COPY (
            SELECT
                strptime(tpep_pickup_datetime,  '%Y-%m-%dT%H:%M:%S.%g') AS pickup_ts,
                strptime(tpep_dropoff_datetime, '%Y-%m-%dT%H:%M:%S.%g') AS dropoff_ts,
                pickup_latitude::DOUBLE  AS pickup_lat,  pickup_longitude::DOUBLE  AS pickup_lng,
                dropoff_latitude::DOUBLE AS dropoff_lat, dropoff_longitude::DOUBLE AS dropoff_lng,
                trip_distance::DOUBLE    AS trip_distance_mi
            FROM read_csv('{tmp_csv.as_posix()}', header=true, all_varchar=true)
        ) TO '{tmp_parquet.as_posix()}' (FORMAT parquet, COMPRESSION zstd)
    """)
    written = con.execute(f"SELECT count(*) FROM read_parquet('{tmp_parquet.as_posix()}')").fetchone()[0]
    con.close()
    if written != rows:
        raise RuntimeError(f"{day}: Parquet has {written} rows, CSV had {rows}")
    tmp_csv.unlink()
    os.replace(tmp_parquet, out)

    return {
        "file": out.relative_to(root).as_posix(),
        "rows": rows,
        "source_count": source_count,
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "bytes": out.stat().st_size,
        "downloaded_at": datetime.now().isoformat(timespec="seconds"),
    }


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = json.loads(path.read_text()) if path.exists() else {
            "source": f"NYC Open Data, 2016 Yellow Taxi Trip Data ({DATASET})",
            "url": f"https://data.cityofnewyork.us/d/{DATASET}",
            "query": {"select": COLUMNS, "window": "1 hour per request, 24 per day", "limit": HOUR_LIMIT},
            "days": {},
        }

    def record(self, day: date, entry: dict) -> None:
        with self.lock:
            self.data["days"][day.isoformat()] = entry
            self.data["total_rows"] = sum(d["rows"] for d in self.data["days"].values())
            tmp = self.path.with_suffix(".json.part")
            tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
            os.replace(tmp, self.path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2016, 6, 30), help="inclusive")
    ap.add_argument("--workers", type=int, default=8, help="days downloaded in parallel")
    ap.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    a = ap.parse_args()

    root: Path = a.data_dir
    (root / "raw").mkdir(parents=True, exist_ok=True)
    manifest = Manifest(root / "raw" / "manifest.json")

    days = [a.start + timedelta(n) for n in range((a.end - a.start).days + 1)]
    todo = [d for d in days if not day_path(root, d).exists()]
    print(f"download: {len(days)} days, {len(days) - len(todo)} already done, {len(todo)} to fetch -> {root}", flush=True)

    t0, done, failed = time.monotonic(), 0, []
    with ThreadPoolExecutor(a.workers) as pool:
        futures = {pool.submit(download_day, d, root): d for d in todo}
        for fut in as_completed(futures):
            d = futures[fut]
            try:
                entry = fut.result()
            except Exception as e:  # noqa: BLE001 - keep going; the day is retried on the next run
                failed.append(d)
                print(f"FAILED {d}: {e}", flush=True)
                continue
            manifest.record(d, entry)
            done += 1
            el = time.monotonic() - t0
            eta = el / done * (len(todo) - done - len(failed))
            print(f"{d}  {entry['rows']:>8,} rows  [{done}/{len(todo)}]  elapsed {el / 60:5.1f} min  "
                  f"eta {eta / 60:5.1f} min", flush=True)

    print(f"download: finished, {done} ok, {len(failed)} failed"
          + (f" (re-run to retry: {', '.join(map(str, failed))})" if failed else ""), flush=True)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
