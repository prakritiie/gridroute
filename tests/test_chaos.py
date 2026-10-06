"""Chaos (spec §7.3 C): areas, who gets hit, recovery timing, the API, and ingest under reordered pings."""
import json
import random

import h3
import pytest
from fastapi.testclient import TestClient

from core import chaos, config
from core.chaos import STALE_KEY, FleetChaos, area_of, command
from sim.transport import PINGS_STREAM
from tests.helpers import TEST_REDIS_URL, now_ms
from tools.audit import audit
from workers.ingest import Ingest

MIDTOWN = (40.7549, -73.9840)
BROOKLYN = (40.6782, -73.9442)


def test_area_is_the_res7_parent_and_its_ring():
    cell9 = h3.latlng_to_cell(*MIDTOWN, config.RES_DRIVER)
    area = area_of(cell9)
    parent = h3.cell_to_parent(cell9, config.RES_DEMAND)
    assert parent in area and len(area) == 7
    assert area_of(parent) == area
    assert area_of(None) is None
    with pytest.raises(ValueError):
        area_of(h3.cell_to_parent(cell9, 5))              # coarser than res 7
    with pytest.raises(ValueError):
        area_of("not-a-cell")


def _positions(n_in=50, n_out=50):
    pos = {f"in{i}": MIDTOWN for i in range(n_in)}
    pos.update({f"out{i}": BROOKLYN for i in range(n_out)})
    return pos


def test_kill_hits_the_share_inside_the_area_only_and_they_come_back():
    fc = FleetChaos()
    cmd = command("kill", h3.latlng_to_cell(*MIDTOWN, 9), pct=0.3, duration_s=120)
    hit = fc.apply(cmd, _positions(), random.Random(1), now=1000.0)
    assert len(hit) == 15 and all(d.startswith("in") for d in hit)
    assert fc.dead == set(hit)
    assert fc.expire(now=1119.0) == [] and fc.dead
    assert sorted(fc.expire(now=1120.0)) == sorted(hit) and not fc.dead


def test_citywide_reorder_then_heal():
    fc = FleetChaos()
    fc.apply(command("reorder", None, pct=0.5, duration_s=60), _positions(), random.Random(2), now=0.0)
    assert len(fc.reorder) == 50
    fc.apply(command("kill", None, pct=0.1), _positions(), random.Random(3), now=0.0)
    fc.apply(command("heal"), {}, random.Random(4), now=1.0)
    assert not fc.reorder and not fc.dead
    fc.apply(command("reorder", None, pct=0.2, duration_s=60), _positions(), random.Random(5), now=0.0)
    fc.expire(now=60.0)
    assert not fc.reorder


def test_burst_is_not_the_fleets_business():
    fc = FleetChaos()
    assert fc.apply(command("burst", None), _positions(), random.Random(1)) == []


def test_ingest_drops_every_reordered_ping_and_counts_it(r):
    """What the demo sends under reorder: each driver's previous ping right after its current one."""
    ing = Ingest(r, consumer="t1")
    t = now_ms()
    for seq, (lat, lng) in enumerate([MIDTOWN, (40.7600, -73.9800)], start=1):
        r.xadd(PINGS_STREAM, {"d": "d1", "lat": lat, "lng": lng, "seq": seq, "t": t + seq})
    r.xadd(PINGS_STREAM, {"d": "d1", "lat": MIDTOWN[0], "lng": MIDTOWN[1], "seq": 1, "t": t})   # late copy
    while ing.step(block_ms=10):
        pass
    r.xadd(PINGS_STREAM, {"d": "d1", "lat": MIDTOWN[0], "lng": MIDTOWN[1], "seq": 1, "t": t})   # later batch
    while ing.step(block_ms=10):
        pass
    assert r.hget("driver:d1", "seq") == "2"                                  # never moved backwards
    assert r.hget("driver:d1", "cell") == h3.latlng_to_cell(40.7600, -73.9800, config.RES_DRIVER)
    assert int(r.get(STALE_KEY)) == 2                                          # one in-batch, one by Lua
    assert audit(r) == []


@pytest.fixture
def client(r, monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", TEST_REDIS_URL)
    from gateway.app import app
    with TestClient(app) as c:
        yield c


def test_post_chaos_appends_a_command(client, r):
    cell = h3.latlng_to_cell(*MIDTOWN, 9)
    resp = client.post("/chaos", json={"type": "kill", "hex": cell, "pct": 0.3, "duration_s": 90})
    assert resp.status_code == 202
    (_, fields), = r.xrange(chaos.STREAM)
    cmd = chaos.parse(fields)
    assert cmd["type"] == "kill" and len(cmd["area"]) == 7 and cmd["duration_s"] == 90
    assert json.loads(r.get("chaos:last"))["type"] == "kill"
    assert [e["kind"] for _, e in r.xrange("stream:events") if e["type"] == "chaos"] == ["kill"]


def test_post_chaos_validates_and_respects_the_lab(client, r):
    assert client.post("/chaos", json={"type": "meteor"}).status_code == 422
    assert client.post("/chaos", json={"type": "kill", "hex": "zzz"}).status_code == 422
    assert client.post("/chaos", json={"type": "kill", "pct": 2}).status_code == 422
    r.set("demo:paused", 1)
    assert client.post("/chaos", json={"type": "heal"}).status_code == 409
