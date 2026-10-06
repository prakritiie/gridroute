"""Policy-lab runner (spec §7.3 B, arrow 4 "runs"): runs replays the dashboard asks for, one at a time.

    python -m workers.runner

  queue     POST /runs pushes a request onto lab:queue; this worker pops them in order
  city      while a replay runs, demo:paused stops the always-on demo (sim.live, sim.riders) and the replay
            starts from a reset Redis; afterwards the state is reset again, the demo resumes and its demand
            history is re-seeded
  record    lab runs are recorded under $DATA/lab/ (the protocol runs in $DATA/runs/ stay the report's input)
  index     every recorded run, protocol and lab, gets run:{id} (config, status, metrics JSON) and a place in
            runs:index, which GET /runs reads

Request keys: lab:req:{req} = {status queued|running|done|failed|cancelled, run_id, request, error}.
Cancel: a queued request is marked cancelled and skipped; a running one sets run:{run_id}:cancel and the
replay stops at its next progress tick (the partial run is recorded and labelled cancelled).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import threading
import time
import traceback
from pathlib import Path

import redis

from core import config, metrics
from eval.report import run_metrics
from tools.reset import reset_sim_state

QUEUE, ACTIVE, INDEX, PAUSED = "lab:queue", "lab:active", "runs:index", "demo:paused"
REQ_TTL_S = 7 * 86_400
PAUSE_SETTLE_S = 3.0                    # the demo checks the pause flag every second
POLICIES = {"greedy-distance", "greedy-eta", "batch-distance", "batch-eta"}


def req_key(req: str) -> str:
    return f"lab:req:{req}"


def _metrics_json(run_dir: Path) -> str:
    try:
        m = run_metrics(run_dir)
    except Exception as e:  # noqa: BLE001 - a run too short to measure still gets listed
        return json.dumps({"error": str(e)[:200]})
    return json.dumps({k: v for k, v in m.items() if not isinstance(v, (bytes, bytearray))}, default=float)


def index_run(r: redis.Redis, run_dir: Path, source: str, status: str | None = None) -> str | None:
    """Store a recorded run's config + metrics in run:{id} and add it to runs:index. Returns the run id."""
    try:
        cfg = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    run_id = cfg["run_id"]
    recorded = int(run_id.rsplit("-", 1)[-1]) if run_id.rsplit("-", 1)[-1].isdigit() else int(time.time())
    fields = {k: str(v) for k, v in cfg.items()} | {"source": source, "metrics": _metrics_json(run_dir)}
    fields["status"] = status or r.hget(f"run:{run_id}", "status") or "done"
    pipe = r.pipeline(transaction=True)
    pipe.hset(f"run:{run_id}", mapping=fields)
    pipe.zadd(INDEX, {run_id: recorded})
    pipe.execute()
    return run_id


def index_all(r: redis.Redis, data_dir: Path) -> int:
    """Index recorded runs that Redis doesn't know yet (a fresh Redis, or runs made from the CLI)."""
    known = set(r.zrange(INDEX, 0, -1))
    n = 0
    for source, sub in (("protocol", "runs"), ("lab", "lab")):
        for d in sorted((data_dir / sub).glob("*")):
            if d.name not in known and (d / "run.json").exists() and index_run(r, d, source):
                n += 1
    return n


def replay_args(req: dict, data_dir: Path) -> argparse.Namespace:
    policy = f"{req['matching']}-{req['cost']}"
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy}")
    return argparse.Namespace(
        day=req["day"], start=req["start"], hours=float(req["hours"]), sample=float(req.get("sample", 0.2)),
        drivers=int(req.get("drivers", 1600)), speed=float(req.get("speed", 10)), seed=int(req.get("seed", 1)),
        policy=policy, reposition=bool(req.get("reposition")), patience_min=5.0,
        drain_min=float(req.get("drain_min", 20)), reset=True, runs_dir=str(data_dir / "lab"),
        gateway_http=config.GATEWAY_HTTP, gateway_ws=config.GATEWAY_WS)


class Runner:
    def __init__(self, r: redis.Redis, data_dir: Path = config.DATA_DIR, settle_s: float = PAUSE_SETTLE_S):
        self.r, self.data_dir, self.settle_s = r, data_dir, settle_s

    def next_request(self, timeout: int = 2) -> dict | None:
        # Under redis-py 8's default 5 s socket timeout: a longer BLPOP would time out the socket.
        got = self.r.blpop([QUEUE], timeout=timeout)
        if not got:
            return None
        req = json.loads(got[1])
        if self.r.hget(req_key(req["req"]), "status") == "cancelled":
            return None
        return req

    def run(self, req: dict, replay_cls=None) -> str | None:
        """Run one request end to end. Returns the run id (None if it never started)."""
        if replay_cls is None:
            from sim.replay import Replay as replay_cls
        key = req_key(req["req"])
        run_id = None
        self.r.set(PAUSED, 1)
        self.r.set(ACTIVE, req["req"])
        self.r.hset(key, mapping={"status": "running", "started": int(time.time())})
        try:
            time.sleep(self.settle_s)                         # let the demo notice the pause
            rp = replay_cls(replay_args(req, self.data_dir))
            run_id = rp.run_id
            self.r.hset(key, "run_id", run_id)
            asyncio.run(rp.run())
            status = "cancelled" if getattr(rp, "cancelled", False) else "done"
            index_run(self.r, self.data_dir / "lab" / run_id, "lab", status)
            self.r.hset(f"run:{run_id}", mapping={"label": req.get("label", ""), "req": req["req"]})
            self.r.hset(key, "status", status)
        except Exception as e:  # noqa: BLE001 - a failed run must never leave the demo paused
            traceback.print_exc()
            self.r.hset(key, mapping={"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]})
            if run_id:
                self.r.hset(f"run:{run_id}", "status", "failed")
        finally:
            self.r.expire(key, REQ_TTL_S)
            self.hand_back()
        return run_id

    def hand_back(self) -> None:
        """Give the city back to the demo: clear the replay's state, unpause, re-seed demand history."""
        reset_sim_state(self.r)
        self.r.delete(PAUSED, ACTIVE)
        try:
            from tools.seed_demand import seed_live
            seed_live(self.r)
        except (OSError, ValueError) as e:
            print(f"runner: demo demand history not re-seeded ({e})", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redis-url", default=config.REDIS_URL)
    a = ap.parse_args()
    metrics.serve_from_env()
    r = redis.Redis.from_url(a.redis_url, decode_responses=True)
    runner = Runner(r)
    if r.get(ACTIVE):                                         # died mid-run last time: release the city
        print("runner: a previous run was interrupted; handing the city back", flush=True)
        r.hset(req_key(r.get(ACTIVE)), "status", "failed")
        runner.hand_back()
    print(f"runner: indexed {index_all(r, config.DATA_DIR)} recorded runs; waiting on {QUEUE}", flush=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            req = runner.next_request()
            if req:
                print(f"runner: starting {req['req']}: {req}", flush=True)
                print(f"runner: finished {runner.run(req)}", flush=True)
        except redis.RedisError as e:
            print(f"runner: {e}", flush=True)
            stop.wait(2.0)


if __name__ == "__main__":
    main()
