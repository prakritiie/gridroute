"""Ingest worker tests against real Redis (spec §14: test_parallel_ingest_ordering; week-1 gate: one-cell invariant)."""
import multiprocessing as mp
import random
import time

import h3

from core import config
from sim.transport import PINGS_STREAM, StreamTransport
from sim.world import World
from tests import race
from tests.helpers import TEST_REDIS_URL, now_ms
from tools.audit import audit
from workers.ingest import GROUP, Ingest


def _xadd(r, d, lat, lng, seq, t):
    r.xadd(PINGS_STREAM, {"d": d, "lat": lat, "lng": lng, "seq": seq, "t": t})


def _drain(ing: Ingest) -> None:
    while ing.step(block_ms=50):
        pass


def test_ingest_applies_latest_ping_and_acks(r):
    ing = Ingest(r, consumer="t1")
    t = now_ms()
    _xadd(r, "d1", 40.7580, -73.9855, 2, t + 4000)
    _xadd(r, "d1", 40.7000, -74.0100, 1, t)               # late, arrives after seq 2 in the same batch
    _xadd(r, "d2", 40.7300, -73.9900, 1, t)
    _drain(ing)

    assert r.hmget("driver:d1", "seq", "cell") == ["2", h3.latlng_to_cell(40.7580, -73.9855, config.RES_DRIVER)]
    assert r.hget("driver:d2", "seq") == "1"
    assert ing.stats.deduped == 1 and ing.stats.applied == 2
    assert r.xpending(PINGS_STREAM, GROUP)["pending"] == 0
    assert audit(r) == []


def test_ingest_seq_across_batches(r):
    """A late ping in a later read is rejected by Lua, not by in-batch dedupe."""
    ing = Ingest(r, consumer="t1")
    t = now_ms()
    _xadd(r, "d1", 40.7580, -73.9855, 5, t)
    _drain(ing)
    _xadd(r, "d1", 40.7000, -74.0100, 4, t + 1)
    _drain(ing)
    assert r.hget("driver:d1", "seq") == "5"
    assert ing.stats.stale == 1


def test_ingest_rejects_invalid(r):
    ing = Ingest(r, consumer="t1")
    t = now_ms()
    _xadd(r, "d1", 51.5, -0.12, 1, t)                     # London: outside NYC bbox
    r.xadd(PINGS_STREAM, {"d": "d2", "lat": "x", "lng": "y", "seq": 1, "t": t})
    r.xadd(PINGS_STREAM, {"lat": 40.75, "lng": -73.98})  # missing fields
    _drain(ing)
    assert ing.stats.invalid == 3 and ing.stats.applied == 0
    assert not r.exists("driver:d1") and not r.exists("driver:d2")
    assert r.xpending(PINGS_STREAM, GROUP)["pending"] == 0  # junk is acked, not retried forever


def test_reclaim_from_dead_consumer(r):
    Ingest(r, consumer="setup")                            # creates the group
    _xadd(r, "d1", 40.7580, -73.9855, 1, now_ms())
    r.xreadgroup(GROUP, "dead", {PINGS_STREAM: ">"}, count=10)   # read, then "crash" without XACK
    assert r.xpending(PINGS_STREAM, GROUP)["pending"] == 1

    ing = Ingest(r, consumer="alive", claim_idle_ms=0)
    assert ing.step(block_ms=50) == 0                      # nothing new for '>'
    assert ing.reclaim() == 1
    assert r.hget("driver:d1", "seq") == "1"
    assert r.xpending(PINGS_STREAM, GROUP)["pending"] == 0


def test_parallel_ingest_ordering(r):
    """Shuffled pings, 2 consumer processes in one group: the highest seq always wins."""
    cells = list(h3.grid_disk(h3.latlng_to_cell(40.758, -73.9855, 9), 4))
    rng = random.Random(11)
    t0 = now_ms()
    pings, final = [], {}
    for i in range(200):
        for seq in range(1, 21):
            lat, lng = h3.cell_to_latlng(rng.choice(cells))
            pings.append((f"d{i}", lat, lng, seq, t0 + seq))
            final[f"d{i}"] = (h3.latlng_to_cell(lat, lng, 9), seq)
    rng.shuffle(pings)
    Ingest(r, consumer="setup")
    pipe = r.pipeline(transaction=False)
    for d, lat, lng, seq, t in pings:
        pipe.xadd(PINGS_STREAM, {"d": d, "lat": lat, "lng": lng, "seq": seq, "t": t})
    pipe.execute()

    start_at = time.time() + 2.0
    with mp.get_context("spawn").Pool(2) as pool:
        handled = pool.starmap(race.ingest_stream_consumer,
                               [(TEST_REDIS_URL, f"c{i}", 100, start_at) for i in range(2)])

    assert sum(handled) == len(pings) and min(handled) > 0  # both consumers did real work
    for d, (cell, seq) in final.items():
        assert r.hmget(f"driver:{d}", "cell", "seq") == [cell, str(seq)]
    assert audit(r, now_ms=t0) == []


def test_one_cell_invariant_5k_drivers(r):
    """Week-1 gate: 5,000 moving drivers, 60 s of pings -> every driver indexed in exactly one cell."""
    w = World(5000, seed=0)
    tx = StreamTransport(r)
    ing = Ingest(r, consumer="t1", batch=5000)
    for tick in range(1, 61):
        w.tick()
        tx.send(w, w.due_pings(), now_ms())
        _drain(ing)
        if tick % 10 == 0:
            assert audit(r) == []

    assert len(list(r.scan_iter("driver:*", count=1000))) == 5000
    assert sum(r.zcard(f"cell:{c}") for c in r.smembers("active_cells")) == 5000
    assert ing.stats.applied == 5000 * 15 and ing.stats.stale == 0
