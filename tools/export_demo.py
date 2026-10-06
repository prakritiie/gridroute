"""Export recorded replays to the static demo site (spec §7.5, arrow 31): no backend, just files.

    python -m tools.export_demo --off $DATA/demo/<R3 run> --on $DATA/demo/<R4 run>

Writes web/public/demo/:
  index.json        the two scenarios (repositioning off / on) with their headline metrics
  off.json, on.json playback captured by sim/playback.py (hex frames, KPIs, matches, forecasts, moves)
  runs.json         every protocol run with its metrics, so the policy lab's comparison works read-only

Build the site with NEXT_PUBLIC_DEMO=1 (docs/demo.md).
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from core import config
from eval.report import run_metrics

OUT = Path(__file__).resolve().parent.parent / "web" / "public" / "demo"


def _metrics(run_dir: Path) -> dict:
    m = run_metrics(run_dir)
    out = {}
    for k, v in m.items():
        if isinstance(v, Decimal):                              # DuckDB sums come back as Decimal
            v = float(v)
        out[k] = round(v, 4) if isinstance(v, float) else v
    return out


def _run_entry(run_dir: Path) -> dict:
    cfg = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    return {"id": cfg["run_id"], "status": "done", "source": "protocol", "label": "", "policy": cfg.get("policy"),
            "day": cfg.get("day"), "start": cfg.get("start"), "hours": float(cfg.get("hours", 0)),
            "speed": float(cfg.get("speed", 0)), "seed": float(cfg.get("seed", 0)),
            "reposition": cfg.get("reposition") == "True", "progress": None, "sim_time": None,
            "requested": None, "requests": float(cfg.get("requests", 0)), "metrics": _metrics(run_dir)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--off", type=Path, required=True, help="run dir recorded with --playback, repositioning off")
    ap.add_argument("--on", type=Path, required=True, help="the same window and seed with repositioning on")
    ap.add_argument("--runs-dir", type=Path, default=config.DATA_DIR / "runs")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)

    scenarios = []
    for key, run_dir, label in (("off", a.off, "Repositioning off"), ("on", a.on, "Repositioning on")):
        pb = json.loads((run_dir / "playback.json").read_text(encoding="utf-8"))
        (a.out / f"{key}.json").write_text(json.dumps(pb, separators=(",", ":")), encoding="utf-8")
        scenarios.append({"key": key, "label": label, "file": f"{key}.json", **pb["meta"], "metrics": _metrics(run_dir)})
        size = (a.out / f"{key}.json").stat().st_size
        print(f"{key}: {len(pb['frames'])} frames, {len(pb['cells']):,} cells, {len(pb['matches']):,} matches, "
              f"{size / 1e6:.1f} MB")
    (a.out / "index.json").write_text(json.dumps({"scenarios": scenarios}, indent=1), encoding="utf-8")

    runs = [_run_entry(d) for d in sorted(a.runs_dir.glob("*")) if (d / "run.json").exists()]
    runs.sort(key=lambda r: r["id"].rsplit("-", 1)[-1], reverse=True)
    (a.out / "runs.json").write_text(json.dumps({"runs": runs, "pending": []}, separators=(",", ":")), encoding="utf-8")
    print(f"runs.json: {len(runs)} protocol runs -> {a.out}")


if __name__ == "__main__":
    main()
