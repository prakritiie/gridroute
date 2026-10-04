"""Cleaning rules (spec §14: test_cleaning_counts): each rule removes exactly the rows planted for it."""
from datetime import datetime, timedelta

import duckdb
import pytest

from data.clean import clean_month, load_rules

T0 = datetime(2016, 6, 7, 8, 0, 0)
MIDTOWN, UNION_SQ = (40.7549, -73.9840), (40.7359, -73.9911)          # ~2.2 km apart


def trip(pick=MIDTOWN, drop=UNION_SQ, dur_s=600, start=T0, dist_mi=1.4):
    return (start, start + timedelta(seconds=dur_s), pick[0], pick[1], drop[0], drop[1], dist_mi)


PLANTED = {
    "zero_or_null_coords": [trip(pick=(0.0, 0.0)), trip(drop=(40.75, 0.0)), trip(pick=(None, -73.98))],
    "outside_nyc_bbox": [trip(drop=(40.6413, -73.30)), trip(pick=(41.20, -73.98))],     # Long Island, upstate
    "duration_out_of_range": [trip(dur_s=30), trip(dur_s=4 * 3600), trip(dur_s=-120), trip(dur_s=0)],
    "too_short": [trip(drop=(40.7550, -73.9841)), trip(drop=(40.7560, -73.9840))],      # ~14 m, ~120 m
    "implausible_speed": [trip(dur_s=3 * 3600 - 60, start=T0 + timedelta(minutes=1)),   # 2.9 km over ~3 h
                          trip(pick=(40.60, -74.10), drop=(40.88, -73.75), dur_s=600)],  # ~44 km in 10 min
}
VALID = [trip(start=T0 + timedelta(minutes=i)) for i in range(5, 25)]
DUPES = 3                                                                # copies of VALID[0]


@pytest.fixture
def raw(tmp_path):
    con = duckdb.connect()
    con.execute("""CREATE TABLE raw (pickup_ts TIMESTAMP, dropoff_ts TIMESTAMP, pickup_lat DOUBLE, pickup_lng DOUBLE,
                                     dropoff_lat DOUBLE, dropoff_lng DOUBLE, trip_distance_mi DOUBLE)""")
    rows = [r for rs in PLANTED.values() for r in rs] + VALID + [VALID[0]] * DUPES
    con.executemany("INSERT INTO raw VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    src = tmp_path / "raw" / "2016-06"
    src.mkdir(parents=True)
    con.execute(f"COPY raw TO '{(src / '2016-06-07.parquet').as_posix()}' (FORMAT parquet)")
    return con, src, tmp_path / "clean" / "2016-06.parquet"


def test_rules_load_in_file_order():
    assert [n for n, _ in load_rules()] == [
        "zero_or_null_coords", "outside_nyc_bbox", "duration_out_of_range", "too_short",
        "implausible_speed", "exact_duplicates"]


def test_cleaning_counts(raw):
    con, src, out = raw
    stats = clean_month(duckdb.connect(), src.as_posix() + "/*.parquet", out)

    expected = {k: len(v) for k, v in PLANTED.items()} | {"exact_duplicates": DUPES}
    assert stats["removed"] == expected
    assert all(n > 0 for n in stats["removed"].values())
    assert stats["raw_rows"] == sum(expected.values()) + len(VALID)
    assert stats["clean_rows"] == len(VALID)


def test_clean_output_is_sorted_with_derived_columns(raw):
    con, src, out = raw
    clean_month(duckdb.connect(), src.as_posix() + "/*.parquet", out)
    rows = duckdb.connect().execute(f"SELECT pickup_ts, duration_s, dist_m FROM '{out.as_posix()}'").fetchall()
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)
    assert all(r[1] == 600 for r in rows)
    assert all(2000 < r[2] < 2400 for r in rows)          # Midtown -> Union Sq straight line
