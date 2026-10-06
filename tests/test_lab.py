"""Policy lab: the /runs API, the runner's queue, pause/hand-back of the city, and run indexing."""
import json

import pytest
from fastapi.testclient import TestClient

from core import config
from tests.helpers import TEST_REDIS_URL
from workers import runner as runner_mod
from workers.runner import ACTIVE, INDEX, PAUSED, QUEUE, Runner, index_all, replay_args


@pytest.fixture
def client(r, monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", TEST_REDIS_URL)
    from gateway.app import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def no_demo_seed(monkeypatch):
    import tools.seed_demand
    monkeypatch.setattr(tools.seed_demand, "seed_live", lambda r, *a, **k: (0, 1.0))


# ---------------------------------------------------------------- API
def test_post_run_queues_a_validated_request(client, r):
    resp = client.post("/runs", json={"day": "2016-06-14", "start": "17:00", "hours": 1.5, "matching": "greedy",
                                      "cost": "distance", "reposition": True, "seed": 3, "label": "evening"})
    assert resp.status_code == 202
    rid = resp.json()["request_id"]
    queued = json.loads(r.lindex(QUEUE, 0))
    assert queued["req"] == rid and queued["matching"] == "greedy" and queued["reposition"] is True
    assert r.hget(f"lab:req:{rid}", "status") == "queued"
    pending = client.get("/runs").json()["pending"]
    assert [p["request_id"] for p in pending] == [rid]


@pytest.mark.parametrize("body, why", [
    ({"day": "2016-05-10"}, "outside the June test month"),
    ({"start": "07:10"}, "not on a 15-minute boundary"),
    ({"start": "22:30", "hours": 2}, "runs past midnight"),
    ({"hours": 5}, "too long"),
    ({"cost": "vibes"}, "unknown cost"),
    ({"speed": 50}, "unsupported speed"),
])
def test_post_run_rejects_bad_requests(client, body, why):
    assert client.post("/runs", json=body).status_code == 422, why


def test_queue_is_capped(client):
    for _ in range(5):
        assert client.post("/runs", json={}).status_code == 202
    assert client.post("/runs", json={}).status_code == 429


def test_cancel_a_queued_request_and_the_runner_skips_it(client, r):
    rid = client.post("/runs", json={}).json()["request_id"]
    assert client.post(f"/runs/{rid}/cancel").json()["was"] == "queued"
    assert Runner(r, settle_s=0).next_request(timeout=1) is None          # popped and skipped
    assert client.post(f"/runs/{rid}/cancel").status_code == 409


def test_cancel_a_running_replay_sets_its_flag(client, r):
    r.hset("lab:req:abc", mapping={"status": "running", "run_id": "run-1"})
    r.hset("run:run-1", "status", "running")
    assert client.post("/runs/abc/cancel").json() == {"cancelled": "run-1", "was": "running"}
    assert r.exists("run:run-1:cancel")


def test_list_runs_shows_indexed_runs_newest_first(client, r):
    r.hset("run:old-1", mapping={"status": "done", "policy": "batch-eta", "hours": "2.0", "source": "protocol",
                                 "reposition": "False", "metrics": json.dumps({"pickup_mean_s": 259.0})})
    r.hset("run:new-2", mapping={"status": "done", "policy": "batch-eta-repos", "reposition": "True",
                                 "source": "lab", "metrics": json.dumps({"pickup_mean_s": 239.0})})
    r.zadd(INDEX, {"old-1": 1, "new-2": 2})
    runs = client.get("/runs").json()["runs"]
    assert [x["id"] for x in runs] == ["new-2", "old-1"]
    assert runs[0]["reposition"] is True and runs[1]["metrics"]["pickup_mean_s"] == 259.0 and runs[1]["hours"] == 2.0
    assert client.get("/runs/old-1").json()["policy"] == "batch-eta"
    assert client.get("/runs/nope").status_code == 404


# ---------------------------------------------------------------- runner
class FakeReplay:
    """Stands in for sim.replay.Replay: records what it saw and writes a run.json like the recorder."""
    seen: list = []

    def __init__(self, a):
        self.a = a
        self.run_id = f"{a.day}-fake-{a.policy}-s{a.seed}-1700000000"
        self.cancelled = False

    async def run(self):
        import redis
        r = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
        FakeReplay.seen.append({"paused": r.exists(PAUSED), "active": r.get(ACTIVE), "policy": self.a.policy,
                                "reposition": self.a.reposition, "runs_dir": self.a.runs_dir})
        from pathlib import Path
        d = Path(self.a.runs_dir) / self.run_id
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({"run_id": self.run_id, "policy": self.a.policy, "hours": "1.0"}))
        return self.run_id


class BrokenReplay(FakeReplay):
    async def run(self):
        raise RuntimeError("OSRM unreachable")


def _req(**kw):
    return {"req": "r1", "day": "2016-06-07", "start": "07:00", "hours": 1.0, "speed": 10, "matching": "batch",
            "cost": "eta", "reposition": True, "seed": 2, "label": "x"} | kw


def test_runner_pauses_the_demo_runs_and_hands_the_city_back(r, tmp_path):
    FakeReplay.seen = []
    r.hset("driver:live-1", "status", "available")                       # demo state the replay must not see
    run_id = Runner(r, data_dir=tmp_path, settle_s=0).run(_req(), replay_cls=FakeReplay)
    seen = FakeReplay.seen[0]
    assert seen["paused"] and seen["active"] == "r1"
    assert seen["policy"] == "batch-eta" and seen["reposition"] is True
    assert seen["runs_dir"] == str(tmp_path / "lab")                    # never the protocol runs folder
    assert not r.exists(PAUSED) and not r.exists(ACTIVE)                # demo resumes
    assert not r.exists("driver:live-1")                                 # state reset on hand-back
    assert r.hget("lab:req:r1", "status") == "done" and r.hget("lab:req:r1", "run_id") == run_id
    assert r.zscore(INDEX, run_id) is not None and r.hget(f"run:{run_id}", "source") == "lab"
    assert "error" in json.loads(r.hget(f"run:{run_id}", "metrics"))   # no parquet: listed, not measured


def test_a_failed_run_never_leaves_the_demo_paused(r, tmp_path):
    Runner(r, data_dir=tmp_path, settle_s=0).run(_req(), replay_cls=BrokenReplay)
    assert not r.exists(PAUSED) and not r.exists(ACTIVE)
    h = r.hgetall("lab:req:r1")
    assert h["status"] == "failed" and "OSRM unreachable" in h["error"]


def test_replay_args_map_the_request(tmp_path):
    a = replay_args(_req(matching="greedy", cost="distance", reposition=False), tmp_path)
    assert (a.policy, a.reposition, a.reset, a.hours) == ("greedy-distance", False, True, 1.0)
    with pytest.raises(ValueError):
        replay_args(_req(matching="random"), tmp_path)


def test_index_all_picks_up_runs_recorded_elsewhere(r, tmp_path):
    for sub, rid in (("runs", "p-1-100"), ("lab", "l-2-200")):
        d = tmp_path / sub / rid
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({"run_id": rid, "policy": "batch-eta"}))
    assert index_all(r, tmp_path) == 2
    assert r.hget("run:p-1-100", "source") == "protocol" and r.hget("run:l-2-200", "source") == "lab"
    assert r.zrange(INDEX, 0, -1) == ["p-1-100", "l-2-200"]             # ordered by recording time
    assert index_all(r, tmp_path) == 0                                   # idempotent
    assert runner_mod.QUEUE == "lab:queue"
