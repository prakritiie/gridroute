"""Playback capture for the static demo: compact frames, deduplicated forecasts and moves, matches."""
import json

from sim.playback import PlaybackRecorder


def _snap(cells):
    return json.dumps({"t": 1, "window_s": 300, "res": {"9": cells, "8": [], "7": []}})


def test_frames_are_compact_and_on_the_sim_clock(r):
    rec = PlaybackRecorder(r, {"run_id": "x"}, frame_sim_s=30)
    r.set("hexes:snapshot", _snap([["89a", 3, 1], ["89b", 0, 2]]))
    r.set("kpis:latest", json.dumps({"drivers_available": 3, "t": 99, "junk": 1}))
    assert rec.tick(0.0)
    assert not rec.tick(10.0)                                  # next frame due at 30 s
    r.set("hexes:snapshot", _snap([["89b", 1, 0], ["89c", 4, 4]]))
    assert rec.tick(31.0)
    out = rec.to_json()
    assert out["cells"] == ["89a", "89b", "89c"]
    assert out["frames"][0]["c"] == [0, 3, 1, 1, 0, 2] and out["frames"][1]["c"] == [1, 1, 0, 2, 4, 4]
    assert out["frames"][0]["k"]["drivers_available"] == 3 and "junk" not in out["frames"][0]["k"]


def test_forecasts_and_moves_are_kept_once_each(r):
    rec = PlaybackRecorder(r, {}, frame_sim_s=10)
    r.set("forecast:snapshot", json.dumps({"slot": 5, "slot_start": "2016-06-07T07:00:00", "version": "v1",
                                           "scale": 0.2, "hexes": {"87a": 10.0}}))
    r.set("reposition:last", json.dumps({"t": 1, "moves": [{"driver_id": "d", "from": "87a", "to": "87b",
                                                            "lat": 0, "lng": 0}]}))
    for s in (0, 10, 20):
        rec.tick(s)
    r.set("forecast:snapshot", json.dumps({"slot": 6, "slot_start": "2016-06-07T07:15:00", "version": "v1",
                                           "scale": 0.2, "hexes": {"87a": 12.0}}))
    rec.tick(30)
    out = rec.to_json()
    assert [f["slot_start"][11:16] for f in out["forecasts"]] == ["07:00", "07:15"]
    assert out["repos"] == [{"s": 0.0, "flows": [["87a", "87b"]]}]


def test_matches_and_write(r, tmp_path):
    rec = PlaybackRecorder(r, {"run_id": "x"})
    rec.match(12.34, {"driver_id": "rd1", "ride_id": "abcdef123456", "pickup": [40.75491, -73.98402], "eta_s": 151})
    path = rec.write(tmp_path / "playback.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["matches"] == [[12.3, 40.75491, -73.98402, 151, "rd1", "abcdef12"]]
    assert data["meta"] == {"run_id": "x"}
