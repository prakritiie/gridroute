"""Capture a replay for the static demo site (spec §7.5, arrow 31): what the dashboard saw, frame by frame.

    python -m sim.replay --policy batch-eta --reposition --playback ...     # writes {run_dir}/playback.json

Every FRAME_SIM_S of sim time inside the window:
  hexes      res-9 cells as one flat int list [cell index, supply, demand, ...] against a shared id table
             (the browser rolls res 8 and 7 up with h3-js, as the aggregator does)
  kpis       the aggregator's KPI frame
And, as they happen: matches (sim s, pickup, eta, driver), forecasts (each new slot) and reposition cycles.
"""
from __future__ import annotations

import json
from pathlib import Path

import redis

FRAME_SIM_S = 30
KPI_KEEP = ("drivers_available", "drivers_busy", "drivers_online", "requests_per_min", "matched_per_min",
            "completed_per_min", "unmatched_per_min", "match_rate_5m", "eta_mean_s", "eta_p90_s", "queue_rides",
            "queue_pings", "double_bookings", "invariant_violations")


class PlaybackRecorder:
    def __init__(self, r: redis.Redis, meta: dict, frame_sim_s: float = FRAME_SIM_S):
        self.r, self.meta, self.every = r, meta, frame_sim_s
        self.cells: dict[str, int] = {}
        self.frames: list[dict] = []
        self.matches: list[list] = []
        self.forecasts: list[dict] = []
        self.repos: list[dict] = []
        self.next_at = 0.0
        self._fc_slot = None
        self._repos_t = None

    def _idx(self, cell: str) -> int:
        i = self.cells.get(cell)
        if i is None:
            i = self.cells[cell] = len(self.cells)
        return i

    def tick(self, sim_s: float) -> bool:
        """Take a frame if one is due. Returns True when it did."""
        if sim_s < self.next_at:
            return False
        self.next_at = sim_s + self.every
        snap, kpis, fc, rep = self.r.mget("hexes:snapshot", "kpis:latest", "forecast:snapshot", "reposition:last")
        flat: list[int] = []
        if snap:
            for cell, s, d in json.loads(snap)["res"]["9"]:
                flat += [self._idx(cell), int(s), int(d)]
        k = json.loads(kpis) if kpis else {}
        self.frames.append({"s": round(sim_s, 1), "c": flat, "k": {x: k.get(x) for x in KPI_KEEP}})
        if fc:
            f = json.loads(fc)
            if f.get("slot") != self._fc_slot:
                self._fc_slot = f.get("slot")
                self.forecasts.append({"s": round(sim_s, 1), "slot_start": f["slot_start"], "version": f["version"],
                                       "scale": f["scale"], "hexes": f["hexes"]})
        if rep:
            rp = json.loads(rep)
            if rp.get("t") != self._repos_t:
                self._repos_t = rp.get("t")
                self.repos.append({"s": round(sim_s, 1), "flows": [[m["from"], m["to"]] for m in rp.get("moves", [])]})
        return True

    def match(self, sim_s: float, msg: dict) -> None:
        p = msg.get("pickup") or [None, None]
        self.matches.append([round(sim_s, 1), round(float(p[0]), 5), round(float(p[1]), 5),
                             int(msg.get("eta_s") or 0), msg.get("driver_id", ""), msg.get("ride_id", "")[:8]])

    def to_json(self) -> dict:
        return {"meta": self.meta, "frame_sim_s": self.every, "cells": list(self.cells), "frames": self.frames,
                "matches": self.matches, "forecasts": self.forecasts, "repos": self.repos}

    def write(self, path: Path) -> Path:
        path.write_text(json.dumps(self.to_json(), separators=(",", ":")), encoding="utf-8")
        return path
