"""Lookup for the June congestion factors built by data/congestion.py."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import h3

from core import config


def hour_of_week(t: datetime) -> int:
    return t.weekday() * 24 + t.hour


class Congestion:
    """factor(lat, lng, how): real time / free-flow time, most specific key first."""

    def __init__(self, rows: list[tuple[str, str, int, float, int]]):
        self.res8, self.res7, self.how = {}, {}, {}
        self.glob = 1.0
        for level, cell, how, factor, _n in rows:
            if level == "res8":
                self.res8[(cell, how)] = factor
            elif level == "res7":
                self.res7[(cell, how)] = factor
            elif level == "how":
                self.how[how] = factor
            else:
                self.glob = factor

    @classmethod
    def load(cls, path: Path | None = None) -> "Congestion":
        path = path or config.DATA_DIR / "clean" / "congestion_june.parquet"
        return cls(duckdb.connect().execute(f"SELECT * FROM '{Path(path).as_posix()}'").fetchall())

    def factor(self, lat: float, lng: float, how: int) -> float:
        r8 = h3.latlng_to_cell(lat, lng, config.RES_ETA)
        if (f := self.res8.get((r8, how))) is not None:
            return f
        if (f := self.res7.get((h3.cell_to_parent(r8, config.RES_DEMAND), how))) is not None:
            return f
        return self.how.get(how, self.glob)
