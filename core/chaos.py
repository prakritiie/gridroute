"""Chaos (spec §7.3 C): break the live demo on purpose and watch the system heal.

    POST /chaos {"type": "kill",    "hex": <any H3 cell>?, "pct": 0.3, "duration_s": 120}
                {"type": "burst",   "hex": ...?, "duration_s": 60}            demand x10 in the area
                {"type": "reorder", "pct": 0.3, "duration_s": 60}             pings arrive out of order
                {"type": "heal"}                                              end everything now

The gateway appends the command to stream:chaos; the demo sim (kill, reorder) and the riders (burst) read
it. An area is the hex's res-7 parent and its 6 neighbours (~36 km²); no hex means the whole city.

  kill      the chosen drivers go silent: no pings, no accepts. The reaper drops them from search within
            30 s and frees their rides; they come back after duration_s and re-index on their first ping.
  burst     riders request 10x their normal rate, from real trips that start in the area.
  reorder   each chosen driver also re-sends its previous ping after the current one; ingest's seq check
            must reject every one of those (stale counter), and the index must never move backwards.
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field

import h3

from core import config

STREAM = "stream:chaos"
TYPES = ("kill", "burst", "reorder", "heal")
BURST_FACTOR = 10
STALE_KEY = "stats:pings_stale"          # ingest's running count of pings the seq check rejected


def area_of(hex_id: str | None) -> set[str] | None:
    """Res-7 cells of the area around any H3 cell (its res-7 parent + ring 1); None = whole city."""
    if not hex_id:
        return None
    if not h3.is_valid_cell(hex_id):
        raise ValueError(f"not an H3 cell: {hex_id}")
    res = h3.get_resolution(hex_id)
    parent = hex_id if res == config.RES_DEMAND else (
        h3.cell_to_parent(hex_id, config.RES_DEMAND) if res > config.RES_DEMAND else None)
    if parent is None:
        raise ValueError("pick a hex at resolution 7 or finer")
    return set(h3.grid_disk(parent, 1))


def in_area(lat: float, lng: float, area: set[str] | None) -> bool:
    return area is None or h3.latlng_to_cell(lat, lng, config.RES_DEMAND) in area


def command(kind: str, hex_id: str | None = None, pct: float = 0.3, duration_s: float = 120) -> dict:
    if kind not in TYPES:
        raise ValueError(f"type must be one of {TYPES}")
    area = area_of(hex_id)
    return {"type": kind, "area": sorted(area) if area else None, "pct": float(pct),
            "duration_s": float(duration_s), "t": int(time.time() * 1000)}


def parse(fields: dict) -> dict:
    """Stream entry fields -> command dict."""
    return json.loads(fields["cmd"])


@dataclass
class FleetChaos:
    """The demo sim's side: who is silenced, who re-sends stale pings, and until when."""
    dead_until: dict[str, float] = field(default_factory=dict)       # driver -> wall time it comes back
    reorder: set[str] = field(default_factory=set)
    reorder_until: float = 0.0

    def apply(self, cmd: dict, positions: dict[str, tuple[float, float]], rng: random.Random,
              now: float | None = None) -> list[str]:
        """Apply one command. positions: driver -> (lat, lng) of drivers that can be hit. Returns who was hit."""
        now = time.time() if now is None else now
        kind = cmd["type"]
        if kind == "heal":
            hit = list(self.dead_until) + sorted(self.reorder)
            self.dead_until.clear()
            self.reorder.clear()
            self.reorder_until = 0.0
            return hit
        if kind not in ("kill", "reorder"):
            return []
        area = set(cmd["area"]) if cmd.get("area") else None
        pool = sorted(d for d, (lat, lng) in positions.items() if in_area(lat, lng, area))
        hit = rng.sample(pool, int(round(len(pool) * min(1.0, max(0.0, cmd.get("pct", 0.3))))))
        until = now + cmd.get("duration_s", 120)
        if kind == "kill":
            for d in hit:
                self.dead_until[d] = max(self.dead_until.get(d, 0.0), until)
        else:
            self.reorder = set(hit)
            self.reorder_until = until
        return hit

    def expire(self, now: float | None = None) -> list[str]:
        """End what has run its course. Returns drivers that come back to life."""
        now = time.time() if now is None else now
        back = [d for d, t in self.dead_until.items() if t <= now]
        for d in back:
            del self.dead_until[d]
        if self.reorder and now >= self.reorder_until:
            self.reorder.clear()
        return back

    @property
    def dead(self) -> set[str]:
        return set(self.dead_until)
