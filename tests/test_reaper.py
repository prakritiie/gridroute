"""Reaper (spec §14: test_reaper_evicts; claim C4)."""
import h3

from core import config
from core.search import candidates
from tests.helpers import CELL_A, CELL_B, now_ms
from tools.audit import audit
from workers.reaper import Reaper

LAT, LNG = h3.cell_to_latlng(CELL_A)


def test_reaper_evicts_only_silent_drivers(r, s):
    t = now_ms()
    s.move_driver("silent", CELL_A, LAT, LNG, seq=1, ts_ms=t - 40_000)
    s.move_driver("fresh", CELL_A, LAT, LNG, seq=1, ts_ms=t)
    s.move_driver("alone", CELL_B, LAT, LNG, seq=1, ts_ms=t - 40_000)

    assert Reaper(r).step(now_ms=t) == 2
    assert r.zrange(f"cell:{CELL_A}", 0, -1) == ["fresh"]
    assert not r.exists(f"cell:{CELL_B}")
    assert r.smembers("active_cells") == {CELL_A}            # emptied cell dropped from the index
    assert audit(r, now_ms=t) == []


def test_killed_driver_leaves_within_35s(r, s):
    """Search hides a silent driver at 30 s (freshness filter); the reaper removes it by 35 s at the latest."""
    t = now_ms()
    s.move_driver("d1", CELL_A, LAT, LNG, seq=1, ts_ms=t)
    reaper = Reaper(r)

    assert [d for d, _ in candidates(r, LAT, LNG, now=t + config.MAX_AGE_MS - 1)] == ["d1"]
    assert candidates(r, LAT, LNG, now=t + config.MAX_AGE_MS + 1) == []

    # Reaper ticks every 5 s; the worst case is a tick just before the 30 s mark
    assert reaper.step(now_ms=t + config.MAX_AGE_MS - 1) == 0
    assert reaper.step(now_ms=t + config.MAX_AGE_MS - 1 + int(config.REAPER_INTERVAL_S * 1000)) == 1
    assert not r.exists(f"cell:{CELL_A}")


def test_reaped_driver_comes_back_on_next_ping(r, s):
    t = now_ms()
    s.move_driver("d1", CELL_A, LAT, LNG, seq=1, ts_ms=t - 40_000)
    Reaper(r).step(now_ms=t)
    assert not r.exists(f"cell:{CELL_A}")
    s.move_driver("d1", CELL_A, LAT, LNG, seq=2, ts_ms=t)
    assert r.zrange(f"cell:{CELL_A}", 0, -1) == ["d1"] and CELL_A in r.smembers("active_cells")
