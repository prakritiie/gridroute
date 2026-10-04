"""Gateway: REST + driver WebSocket, against real Redis (DB 15)."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from core import config
from core.scripts import Claim
from tests.helpers import CELL_A, TEST_REDIS_URL, now_ms, queue_ride


@pytest.fixture
def client(r, monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", TEST_REDIS_URL)
    from gateway.app import app
    with TestClient(app) as c:
        yield c


def test_post_ride_queues_and_enqueues(client, r):
    resp = client.post("/rides", json={"lat": 40.7549, "lng": -73.984, "dropoff_lat": 40.76, "dropoff_lng": -73.97})
    assert resp.status_code == 202
    ride_id = resp.json()["ride_id"]
    ride = r.hgetall(f"ride:{ride_id}")
    assert ride["status"] == "queued" and ride["dlat"] == "40.76" and ride["attempts"] == "0"
    assert 0 < r.ttl(f"ride:{ride_id}") <= 24 * 3600
    (_, fields), = r.xrange("stream:rides")
    assert fields["ride_id"] == ride_id

    got = client.get(f"/rides/{ride_id}").json()
    assert got["ride_id"] == ride_id and got["status"] == "queued"


def test_rejects_rides_outside_nyc_and_unknown_ids(client):
    assert client.post("/rides", json={"lat": 51.5, "lng": -0.12}).status_code == 422
    assert client.post("/rides", json={"lat": "x"}).status_code == 422
    assert client.get("/rides/nope").status_code == 404


def test_hexes_empty_until_aggregator_runs_and_healthz(client):
    assert client.get("/hexes").json()["cells"] == []
    assert client.get("/healthz").json() == {"ok": True}


def _sync(ws):
    """Round trip that proves every earlier message on this socket was processed."""
    ws.send_json({"type": "trip_done", "driver_id": "sync", "ride_id": "sync"})
    assert ws.receive_json() == {"type": "trip_done_ack", "ride_id": "sync", "ok": False}


def test_ws_pings_land_in_stream_and_bad_ones_are_dropped(client, r):
    t = now_ms()
    with client.websocket_connect("/ws/driver") as ws:
        ws.send_json({"type": "pings", "pings": [
            ["d1", 40.75, -73.98, 1, t],
            ["d2", 40.76, -73.97, 7, t],
            ["d3", 51.50, -0.12, 1, t],          # outside NYC
            ["d4", "x", -73.97, 1, t],           # junk
        ]})
        _sync(ws)
    entries = [f for _, f in r.xrange("stream:pings")]
    assert [(e["d"], e["seq"]) for e in entries] == [("d1", "1"), ("d2", "7")]


def test_offer_reaches_the_drivers_socket(client, r):
    with client.websocket_connect("/ws/driver") as ws:
        ws.send_json({"type": "register", "driver_ids": ["d1", "d2"]})
        _sync(ws)
        offer = {"type": "assignment", "driver_id": "d2", "ride_id": "r9", "pickup": [40.75, -73.98], "eta_s": 60}
        deadline = time.time() + 5
        while r.publish("ch:offers", json.dumps(offer)) < 1:     # wait for the fan-out subscription
            assert time.time() < deadline
            time.sleep(0.05)
        assert ws.receive_json() == offer


def test_trip_done_releases_driver(client, r, s):
    t = now_ms()
    s.move_driver("d1", CELL_A, 40.75, -73.98, seq=1, ts_ms=t)
    queue_ride(r, "r1")
    assert s.claim_driver("d1", "r1", now_ms=t) is Claim.CLAIMED
    with client.websocket_connect("/ws/driver") as ws:
        ws.send_json({"type": "trip_done", "driver_id": "d1", "ride_id": "wrong"})
        assert ws.receive_json()["ok"] is False
        ws.send_json({"type": "trip_done", "driver_id": "d1", "ride_id": "r1"})
        assert ws.receive_json() == {"type": "trip_done_ack", "ride_id": "r1", "ok": True}
    assert r.hget("driver:d1", "status") == "available"
    assert r.hget("ride:r1", "status") == "completed"
    assert r.xrange("stream:events")[-1][1]["type"] == "complete"


def test_hexes_endpoint_reads_snapshot_at_resolution(client, r):
    snap = {"t": 1, "window_s": 300, "res": {"9": [["a", 1, 2]], "8": [["b", 3, 4]], "7": [["c", 5, 6]]}}
    r.set("hexes:snapshot", json.dumps(snap))
    assert client.get("/hexes?res=8").json() == {"res": 8, "t": 1, "window_s": 300, "cells": [["b", 3, 4]]}
    assert client.get("/hexes?res=5").status_code == 422


def test_ops_socket_gets_latest_state_then_live_frames(client, r):
    r.set("kpis:latest", json.dumps({"drivers_available": 7}))
    r.set("hexes:snapshot", json.dumps({"t": 1, "window_s": 300, "res": {"9": []}}))
    with client.websocket_connect("/ws/ops") as ws:
        assert ws.receive_json() == {"type": "kpis", "data": {"drivers_available": 7}}
        assert ws.receive_json()["type"] == "hexes"
        deadline = time.time() + 5
        while r.publish("ch:kpis", json.dumps({"drivers_available": 8})) < 1:
            assert time.time() < deadline
            time.sleep(0.05)
        assert ws.receive_json() == {"type": "kpis", "data": {"drivers_available": 8}}
        offer = {"type": "assignment", "driver_id": "d1", "ride_id": "r1", "pickup": [40.75, -73.98], "eta_s": 42}
        r.publish("ch:offers", json.dumps(offer))
        assert ws.receive_json() == {"type": "match", "data": offer}


def test_metrics_and_cors(client):
    client.post("/rides", json={"lat": 40.75, "lng": -73.98})
    body = client.get("/metrics").text
    assert "rides_requested_total" in body and "driver_sockets" in body
    pre = client.options("/hexes", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"})
    assert pre.headers["access-control-allow-origin"] == "http://localhost:3000"
