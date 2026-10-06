"""Results table (spec §8.4) from recorded runs.

    python -m eval.report                       # every run under $GRIDROUTE_DATA_DIR/runs
    python -m eval.report --write docs/results.md

Per run, over rides requested after the warm-up (the fleet starts from a cold, artificial state):
  pickup mean / p90   true simulated drive time to the pickup (OSRM x June congestion), not a model's guess
  wait mean           request -> driver at the pickup (includes time to be matched)
  unmatched %         rides that never got a driver (unmatched, expired, or still queued at the end)
  empty km / drv-h    km driven without a rider, per driver-hour of the run
  decision p99        matcher wall time per batch
Rows group runs by policy; with several seeds a cell shows mean ± half the min-max spread.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from core import config

ROWS = {  # spec §8.4: each row changes exactly one thing
    "greedy-distance": ("R0", "Greedy", "Distance", "Off"),
    "greedy-eta": ("R1", "Greedy", "ETA model", "Off"),
    "batch-distance": ("R2", "Batch (Hungarian)", "Distance", "Off"),
    "batch-eta": ("R3", "Batch", "ETA model", "Off"),
    "batch-eta-repos": ("R4", "Batch", "ETA model", "On"),
}
WARMUP_S = 15 * 60


def run_metrics(run_dir: Path, warmup_s: float = WARMUP_S) -> dict:
    cfg = json.loads((run_dir / "run.json").read_text())
    con = duckdb.connect()
    ev, rd = (run_dir / "events.parquet").as_posix(), (run_dir / "rides.parquet").as_posix()
    window_s = float(cfg["window_s"])
    con.execute(f"CREATE VIEW rides AS SELECT * FROM '{rd}' WHERE req_sim >= {warmup_s} AND req_sim < {window_s}")
    con.execute(f"CREATE VIEW ev AS SELECT * FROM '{ev}'")
    n, unmatched = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE status IN ('unmatched', 'expired', 'queued')) FROM rides").fetchone()
    pick = con.execute("""
        SELECT avg(leg_s), quantile_cont(leg_s, 0.9), avg(wait_s), avg(match_s), count(*), sum(leg_s) / nullif(sum(osrm_s), 0)
        FROM ev JOIN rides ON ev.ride = rides.ride WHERE ev.type = 'pickup'""").fetchone()
    p99 = con.execute("SELECT quantile_cont(ms, 0.99), count(*) FROM ev WHERE type = 'batch'").fetchone()
    # How well the matcher's own pickup estimate matched the true (simulated) drive: the ETA model online
    cols = [c[0] for c in con.execute("DESCRIBE rides").fetchall()]
    eta_err = con.execute("""
        SELECT avg(abs(rides.eta_pred - ev.leg_s)) FROM ev JOIN rides ON ev.ride = rides.ride
        WHERE ev.type = 'pickup' AND rides.eta_pred IS NOT NULL""").fetchone()[0] if "eta_pred" in cols else None
    fleet = con.execute(
        "SELECT drivers, sim_hours, empty_km, loaded_km, conflicts FROM ev WHERE type = 'fleet' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    empty_per_h = fleet[2] / (fleet[0] * fleet[1]) if fleet and fleet[0] else None
    ev_cols = [c[0] for c in con.execute("DESCRIBE ev").fetchall()]
    repos = con.execute("SELECT repos_km, repositions FROM ev WHERE type = 'fleet' ORDER BY id DESC LIMIT 1"
                        ).fetchone() if "repos_km" in ev_cols else None
    # Supply-demand mismatch: how many drivers sat idle while riders gave up? Busy = matched - completed,
    # reconstructed from event wall times; compared per 5-sim-minute slot with that slot's unmatched rate.
    speed = float(cfg.get("speed", 10))
    idle_when_unmatched = con.execute(f"""
        WITH t0 AS (SELECT min(t) AS t0 FROM ev WHERE type = 'match'),
             d AS (SELECT floor((t - t0) * {speed} / 1000 / 300) AS slot,
                          sum(CASE WHEN type = 'match' THEN 1 ELSE -1 END) AS dv
                   FROM ev, t0 WHERE type IN ('match', 'complete') GROUP BY slot),
             busy AS (SELECT slot, sum(dv) OVER (ORDER BY slot) AS busy FROM d),
             um AS (SELECT floor(req_sim / 300) AS slot,
                           avg(CASE WHEN status IN ('unmatched', 'expired', 'queued') THEN 1 ELSE 0 END) AS rate
                    FROM rides GROUP BY slot)
        SELECT median({fleet[0] if fleet else 0} - busy.busy) FROM um JOIN busy USING (slot) WHERE um.rate >= 0.10
    """).fetchone()[0]
    return {
        "run_id": cfg["run_id"], "policy": cfg.get("policy", "?"), "seed": cfg.get("seed"),
        "requests": n, "picked_up": pick[4],
        "pickup_mean_s": pick[0], "pickup_p90_s": pick[1], "wait_mean_s": pick[2], "match_wait_s": pick[3],
        "congestion_x": pick[5],
        "unmatched_pct": 100 * unmatched / n if n else None,
        "empty_km_per_driver_h": empty_per_h,
        "decision_p99_ms": p99[0], "batches": p99[1],
        "conflicts": int(fleet[4]) if fleet else None,
        "idle_when_unmatched": idle_when_unmatched,
        "eta_mae_s": eta_err,
        "repos_km_per_driver_h": repos[0] / (fleet[0] * fleet[1]) if repos and repos[0] is not None and fleet
        and fleet[0] else None,
        "repositions": int(repos[1]) if repos and repos[1] is not None else None,
    }


def _cell(vals: list[float | None], fmt: str) -> str:
    vals = [v for v in vals if v is not None]
    if not vals:
        return ""
    mean = sum(vals) / len(vals)
    if len(vals) == 1:
        return format(mean, fmt)
    return f"{format(mean, fmt)} ± {format((max(vals) - min(vals)) / 2, fmt)}"


def mmss(s: float) -> str:
    return f"{int(s // 60)}:{int(round(s % 60)):02d}"


def table(metrics: list[dict]) -> str:
    lines = ["| Row | Matching | Cost | Repositioning | Seeds | Pickup mean | Pickup p90 | Unmatched % | "
             "Empty km / driver-h | Decision p99 | Pickup-estimate error |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for policy, (row, match, cost, repos) in ROWS.items():
        ms = [m for m in metrics if m["policy"] == policy]
        if not ms:
            lines.append(f"| {row} | {match} | {cost} | {repos} | | | | | | | |")
            continue
        pm = [m["pickup_mean_s"] for m in ms]
        p90 = [m["pickup_p90_s"] for m in ms]
        lines.append(
            f"| **{row}** | {match} | {cost} | {repos} | {len(ms)} | "
            f"{mmss(sum(pm) / len(pm))}{' ± ' + mmss((max(pm) - min(pm)) / 2) if len(ms) > 1 else ''} | "
            f"{mmss(sum(p90) / len(p90))}{' ± ' + mmss((max(p90) - min(p90)) / 2) if len(ms) > 1 else ''} | "
            f"{_cell([m['unmatched_pct'] for m in ms], '.2f')} | "
            f"{_cell([m['empty_km_per_driver_h'] for m in ms], '.2f')} | "
            f"{_cell([m['decision_p99_ms'] for m in ms], '.0f')} ms | "
            f"{mmss(sum(e) / len(e)) if (e := [m['eta_mae_s'] for m in ms if m['eta_mae_s'] is not None]) else ''} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs-dir", type=Path, default=config.DATA_DIR / "runs")
    ap.add_argument("--write", type=Path, help="also write the table + per-run details to this markdown file")
    a = ap.parse_args()
    runs = sorted(p for p in a.runs_dir.glob("*") if (p / "run.json").exists())
    metrics = [run_metrics(p) for p in runs]
    print(table(metrics))
    print()
    for m in metrics:
        print(f"{m['run_id']}: {m['requests']:,} rides, {m['picked_up']:,} picked up, pickup {mmss(m['pickup_mean_s'])} "
              f"(true/free-flow {m['congestion_x']:.2f}x), wait {mmss(m['wait_mean_s'])}, "
              f"unmatched {m['unmatched_pct']:.2f}%, conflicts {m['conflicts']}"
              + (f", median {m['idle_when_unmatched']:.0f} drivers idle in slots with >= 10% unmatched"
                 if m["idle_when_unmatched"] is not None else ""))
    if a.write:
        detail = "\n".join(
            f"| `{m['run_id']}` | {m['requests']:,} | {m['picked_up']:,} | {mmss(m['pickup_mean_s'])} | "
            f"{mmss(m['wait_mean_s'])} | {m['unmatched_pct']:.2f}% | {m['empty_km_per_driver_h']:.2f} | "
            f"{m['decision_p99_ms']:.0f} ms | {m['conflicts']} |" for m in metrics)
        rm = [m for m in metrics if m.get("repositions") is not None]
        repos = "\n".join(f"- `{m['run_id']}`: {m['repositions']:,} moves, "
                           f"{m['repos_km_per_driver_h']:.2f} km per driver-hour spent repositioning" for m in rm)
        a.write.write_text(RESULTS_DOC.format(table=table(metrics), detail=detail, repos=repos), encoding="utf-8")
        print(f"wrote {a.write}")


RESULTS_DOC = """# Results

_Generated by `python -m eval.report --write docs/results.md` from recorded replay runs. Do not edit by hand._

## Policy comparison (spec §8.4)

{table}

**Pickup time** is the simulated true drive time to the pickup: OSRM road time × a June congestion factor
for the pickup's hex and hour of week. The ETA model never sees June, so the replay cannot grade its own
homework (both still share OSRM as a base). Rides requested in the first 15 sim-minutes are excluded
(cold start). Idle drivers wait at their last drop-off, so in R0–R3 empty km is exactly the distance driven to
pickups; in R4 it also includes the repositioning drives (including ones an offer cut short).

## Repositioning (R4)

R4 is R3 plus the demand forecaster and the repositioner. The replay seeds the 28 days of real request counts
before the window, so the forecaster runs from the first slot. Every 2 sim-minutes the repositioner sends 20%
of a hex's idle-driver surplus over its forecast toward the neighbouring hex with the largest expected
shortfall (at most 60 drivers per cycle; a sent driver is left alone for 10 sim-minutes). Drivers stay
available while they drive, so an offer on the way wins.

{repos}

## Runs

| Run | Rides | Picked up | Pickup mean | Wait mean | Unmatched | Empty km / drv-h | Decision p99 | Conflicts |
|---|---|---|---|---|---|---|---|---|
{detail}

**Protocol (v0).** Tuesday 7 June 2016, 07:00–09:00 (morning peak), a seeded 20% sample of real yellow-cab
requests at their real pickup times, 1,600 drivers placed at real drop-offs from 06:00–07:00, time compressed
10×, riders wait up to 5 sim-minutes for a driver, 20 sim-minutes of drain after the window. The full protocol (3 weekdays + 1 weekend day, 3 seeds per row)
fills in as runs accumulate.
"""


if __name__ == "__main__":
    main()
