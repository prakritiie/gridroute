"""Ring search correctness (spec §14: test_ring_matches_bruteforce, claim C3)."""
import h3
import numpy as np

from core.scripts import Scripts
from core.search import brute_force, candidates
from tests.helpers import now_ms
from sim.world import M_PER_DEG_LAT

CENTER = (40.7549, -73.9840)       # Midtown


def _seed_drivers(r, n: int, radius_m: float, now: int, seed: int = 0) -> None:
    rng = np.random.default_rng(seed)
    s = Scripts(r)
    rad = radius_m * np.sqrt(rng.uniform(0, 1, n))
    th = rng.uniform(0, 2 * np.pi, n)
    lat = CENTER[0] + rad * np.cos(th) / M_PER_DEG_LAT
    lng = CENTER[1] + rad * np.sin(th) / (M_PER_DEG_LAT * np.cos(np.radians(CENTER[0])))
    s.move_drivers([(f"d{i}", h3.latlng_to_cell(a, b, 9), float(a), float(b), 1, now)
                    for i, (a, b) in enumerate(zip(lat, lng))])


def _queries(k: int, radius_m: float, seed: int = 1):
    rng = np.random.default_rng(seed)
    rad = radius_m * np.sqrt(rng.uniform(0, 1, k))
    th = rng.uniform(0, 2 * np.pi, k)
    return zip(CENTER[0] + rad * np.cos(th) / M_PER_DEG_LAT,
               CENTER[1] + rad * np.sin(th) / (M_PER_DEG_LAT * np.cos(np.radians(CENTER[0]))))


def _compare(r, now, guard: bool, n_queries: int = 500, need: int = 5) -> tuple[int, int]:
    # Reference: every fresh indexed driver, ranked by the same haversine (loaded once, ranked in NumPy)
    everyone = brute_force(r, *CENTER, need=10**9, radius_m=float("inf"), now=now)
    ids = np.array([d for d, _ in everyone])
    pos = np.array([[float(x) for x in r.hmget(f"driver:{d}", "lat", "lng")] for d in ids])
    p_lat, p_lng = np.radians(pos[:, 0]), np.radians(pos[:, 1])

    wrong = short = 0
    for lat, lng in _queries(n_queries, 2500):
        ring = candidates(r, lat, lng, need=need, kmax=3, now=now, guard=guard)
        if len(ring) < need:
            short += 1
            continue
        q_lat, q_lng = np.radians(lat), np.radians(lng)
        a = np.sin((p_lat - q_lat) / 2) ** 2 + np.cos(q_lat) * np.cos(p_lat) * np.sin((p_lng - q_lng) / 2) ** 2
        order = np.lexsort((ids, a))[:need]                     # same tie-break as rank_by_haversine
        if [d for d, _ in ring] != list(ids[order]):
            wrong += 1
    print(f"guard={guard}: {wrong}/{n_queries} wrong, {short} short")
    return wrong, short


def test_ring_matches_bruteforce(r):
    now = now_ms()
    _seed_drivers(r, 1500, radius_m=3000, now=now)
    wrong, short = _compare(r, now, guard=True)
    assert short < 10            # density is high enough that almost every query finds 5 drivers
    assert wrong == 0


def test_without_guard_ring_search_is_wrong(r):
    """Control: stopping at the first ring that has enough drivers returns the wrong top-N."""
    now = now_ms()
    _seed_drivers(r, 1500, radius_m=3000, now=now)
    wrong, _ = _compare(r, now, guard=False)
    assert wrong > 25


def test_stale_drivers_never_returned(r):
    now = now_ms()
    s = Scripts(r)
    cell = h3.latlng_to_cell(*CENTER, 9)
    s.move_driver("fresh", cell, *CENTER, seq=1, ts_ms=now - 1000)
    s.move_driver("stale", cell, *CENTER, seq=1, ts_ms=now - 31_000)
    assert [d for d, _ in candidates(r, *CENTER, need=5, now=now)] == ["fresh"]


def test_empty_area_returns_nothing(r):
    assert candidates(r, 40.60, -73.80, need=5) == []
