"""The three ETA baselines the model must beat (spec §8.1). All fitted on the training months only.

  B1  straight-line distance / city-wide median speed
  B2  raw OSRM free-flow duration
  B3  OSRM distance / historical median speed for (pickup res-8 hex, hour of week); sparse keys fall back
      to (res-7 hex, hour of week), then hour of week, then city-wide (each key needs >= MIN_N trips)
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import h3
import numpy as np

from core import config

MIN_N = 20


class Baselines:
    def __init__(self, city_speed: float, b3: dict):
        self.city_speed = city_speed
        self.b3 = b3          # {"res8": {(hex, how): mps}, "res7": {...}, "how": {how: mps}, "global": mps}

    @classmethod
    def fit(cls, lat, lng, how, straight_m, osrm_m, duration_s) -> "Baselines":
        ok = (duration_s > 0) & np.isfinite(osrm_m)
        city = float(np.median(straight_m[ok] / duration_s[ok]))
        speed = osrm_m[ok] / duration_s[ok]
        r8 = np.array([h3.latlng_to_cell(a, b, config.RES_ETA) for a, b in zip(lat[ok].tolist(), lng[ok].tolist())])
        r7 = np.array([h3.cell_to_parent(c, config.RES_DEMAND) for c in r8])
        con = duckdb.connect()
        con.register("t", {"r8": r8, "r7": r7, "how": how[ok].astype(np.int64), "v": speed})
        b3 = {"global": float(np.median(speed))}
        for level, key in (("res8", "r8"), ("res7", "r7")):
            b3[level] = {(c, int(h)): float(v) for c, h, v in con.execute(
                f"SELECT {key}, how, median(v) FROM t GROUP BY ALL HAVING count(*) >= {MIN_N}").fetchall()}
        b3["how"] = {int(h): float(v) for h, v in con.execute(
            f"SELECT how, median(v) FROM t GROUP BY how HAVING count(*) >= {MIN_N}").fetchall()}
        return cls(city, b3)

    def b1(self, straight_m):
        return straight_m / self.city_speed

    @staticmethod
    def b2(osrm_s):
        return osrm_s

    def b3_speed(self, lat: float, lng: float, how: int) -> float:
        r8 = h3.latlng_to_cell(lat, lng, config.RES_ETA)
        if (v := self.b3["res8"].get((r8, how))) is not None:
            return v
        if (v := self.b3["res7"].get((h3.cell_to_parent(r8, config.RES_DEMAND), how))) is not None:
            return v
        return self.b3["how"].get(how, self.b3["global"])

    def b3_predict(self, lat, lng, how, osrm_m):
        speeds = np.array([self.b3_speed(a, b, int(h)) for a, b, h in zip(lat.tolist(), lng.tolist(), how.tolist())])
        return osrm_m / speeds

    def save(self, path: Path) -> None:
        enc = lambda d: [[k[0], k[1], v] for k, v in d.items()]  # noqa: E731
        path.write_text(json.dumps({"city_speed": self.city_speed, "global": self.b3["global"],
                                    "res8": enc(self.b3["res8"]), "res7": enc(self.b3["res7"]),
                                    "how": [[k, v] for k, v in self.b3["how"].items()]}))

    @classmethod
    def load(cls, path: Path) -> "Baselines":
        j = json.loads(path.read_text())
        return cls(j["city_speed"], {"global": j["global"],
                                     "res8": {(c, h): v for c, h, v in j["res8"]},
                                     "res7": {(c, h): v for c, h, v in j["res7"]},
                                     "how": {h: v for h, v in j["how"]}})
