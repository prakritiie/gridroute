import os
import time

import h3
import redis

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

# Two adjacent res-9 cells in Midtown Manhattan
CELL_A = h3.latlng_to_cell(40.7580, -73.9855, 9)
CELL_B = h3.grid_ring(CELL_A, 1)[0]


def now_ms() -> int:
    return int(time.time() * 1000)


def queue_ride(r: redis.Redis, ride_id: str, lat: float = 40.758, lng: float = -73.9855) -> None:
    """What the gateway does on POST /rides (minus the stream append)."""
    r.hset(f"ride:{ride_id}", mapping={"status": "queued", "lat": lat, "lng": lng, "t_req": now_ms(), "attempts": 0})
