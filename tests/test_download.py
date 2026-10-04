"""Downloader tests with a fake API (no network)."""
import json
import urllib.parse
from datetime import date

import duckdb
import pytest

from data.download import COLUMNS, Manifest, day_path, download_day

HEADER = ",".join(f'"{c}"' for c in COLUMNS)


def _row(h: int, i: int) -> str:
    return (f'"2016-01-12T{h:02d}:{i:02d}:00.000","2016-01-12T{h:02d}:{i + 10:02d}:00.000",'
            f'"40.75{i}","-73.98{i}","40.76{i}","-73.97{i}","1.{i}"')


def fake_api(rows_per_hour: int, reported_count: int | None = None):
    calls = []

    def fetch(url: str) -> str:
        calls.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if q["$select"][0].startswith("count"):
            return json.dumps([{"n": str(reported_count if reported_count is not None else rows_per_hour * 24)}])
        h = int(q["$where"][0].split("T")[1][:2])
        return "\n".join([HEADER] + [_row(h, i) for i in range(rows_per_hour)]) + "\n"

    return fetch, calls


def test_download_day_writes_typed_parquet(tmp_path):
    fetch, calls = fake_api(rows_per_hour=3)
    entry = download_day(date(2016, 1, 12), tmp_path, fetch=fetch)

    out = day_path(tmp_path, date(2016, 1, 12))
    assert out.exists() and not list(out.parent.glob("*.part"))
    assert entry["rows"] == entry["source_count"] == 72 and len(calls) == 25
    con = duckdb.connect()
    schema = {row[0]: row[1] for row in con.execute(f"DESCRIBE SELECT * FROM '{out.as_posix()}'").fetchall()}
    assert schema["pickup_ts"] == "TIMESTAMP" and schema["pickup_lat"] == "DOUBLE"
    first = con.execute(f"SELECT min(pickup_ts), max(pickup_ts) FROM '{out.as_posix()}'").fetchone()
    assert str(first[0]) == "2016-01-12 00:00:00" and str(first[1]) == "2016-01-12 23:02:00"


def test_hourly_windows_cover_the_day_without_overlap(tmp_path):
    fetch, calls = fake_api(rows_per_hour=0)
    download_day(date(2016, 3, 13), tmp_path, fetch=fetch)
    wheres = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query)["$where"][0] for u in calls[:24]]
    assert "'2016-03-13T00:00:00'" in wheres[0] and "< '2016-03-13T01:00:00'" in wheres[0]
    assert "< '2016-03-14T00:00:00'" in wheres[23]
    for a, b in zip(wheres, wheres[1:]):
        assert a.split("< ")[1] == b.split(">= ")[1].split(" AND")[0]   # each window ends where the next starts


def test_count_mismatch_fails_and_leaves_no_file(tmp_path):
    fetch, _ = fake_api(rows_per_hour=2, reported_count=49)
    with pytest.raises(RuntimeError, match="API reports 49"):
        download_day(date(2016, 1, 12), tmp_path, fetch=fetch)
    out = day_path(tmp_path, date(2016, 1, 12))
    assert not out.exists() and not list(out.parent.glob("*"))


def test_manifest_records_days(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    m.record(date(2016, 1, 1), {"rows": 10})
    m.record(date(2016, 1, 2), {"rows": 5})
    data = json.loads((tmp_path / "manifest.json").read_text())
    assert data["total_rows"] == 15 and set(data["days"]) == {"2016-01-01", "2016-01-02"}
