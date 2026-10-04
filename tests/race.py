"""Process entry points for the concurrency tests. Kept in their own module so spawned processes can import them."""
import random
import time

import redis

from core.scripts import Claim, Scripts
from tests.helpers import now_ms


def _wait_until(start_at: float) -> None:
    while time.time() < start_at:
        pass


def lua_matcher(url: str, worker: int, ride_ids: list[str], driver_ids: list[str], start_at: float) -> int:
    """Acts like one matcher process: for each ride, try candidates until claimed or the ride is handled."""
    rng = random.Random(worker)
    r = redis.Redis.from_url(url, decode_responses=True)
    s = Scripts(r)
    rides, drivers = ride_ids[:], driver_ids[:]
    rng.shuffle(rides)
    seen_taken: set[str] = set()
    claimed = 0
    _wait_until(start_at)
    for ride in rides:
        rng.shuffle(drivers)
        for d in drivers:
            if d in seen_taken:
                continue
            res = s.claim_driver(d, ride, now_ms=now_ms(), lease_ms=60_000)
            if res is Claim.CLAIMED:
                claimed += 1
                break
            if res is Claim.RIDE_HANDLED:
                break
            seen_taken.add(d)
    return claimed


def naive_matcher(url: str, worker: int, ride_ids: list[str], driver_ids: list[str], start_at: float) -> int:
    """The race-prone version: check, then take, in separate round trips. Same order in every process."""
    r = redis.Redis.from_url(url, decode_responses=True)
    claimed = 0
    _wait_until(start_at)
    for ride in ride_ids:
        for d in driver_ids:
            if r.hget(f"ride:{ride}", "status") != "queued":
                break
            if r.hget(f"driver:{d}", "status") != "available":
                continue
            time.sleep(0.001)                               # the gap every real network hop has
            r.set(f"lock:driver:{d}", ride, px=60_000)
            r.hset(f"driver:{d}", mapping={"status": "assigned", "ride": ride})
            r.hset(f"ride:{ride}", mapping={"status": "assigned", "driver": d})
            claimed += 1
            break
    return claimed


def matcher_process(url: str, consumer: str, start_at: float) -> dict:
    """Runs the real matcher loop until the ride stream has nothing left for it."""
    from dataclasses import asdict

    from workers.matcher import Matcher

    m = Matcher(redis.Redis.from_url(url, decode_responses=True), consumer=consumer, batch=20)
    _wait_until(start_at)
    idle = 0
    while idle < 3:                       # requeued rides reappear immediately, so wait for 3 empty reads
        idle = idle + 1 if m.step(block_ms=100) == 0 else 0
    return asdict(m.stats)


def ingest_stream_consumer(url: str, consumer: str, batch: int, start_at: float) -> int:
    """Runs the real ingest worker loop until the stream has nothing new for it."""
    from workers.ingest import Ingest

    ing = Ingest(redis.Redis.from_url(url, decode_responses=True), consumer=consumer, batch=batch)
    handled = 0
    _wait_until(start_at)
    while n := ing.step(block_ms=300):
        handled += n
    return handled


def ingest_consumer(url: str, pings: list[tuple]) -> int:
    """Applies a shuffled slice of pings, like one of several ingest consumers."""
    s = Scripts(redis.Redis.from_url(url, decode_responses=True))
    return sum(s.move_driver(*p) for p in pings)
