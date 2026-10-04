"""Shared fixtures. Tests run against a real Redis 7 (docker compose up -d redis), DB 15, flushed per test.

If Redis is unreachable the Redis tests are skipped locally; CI sets GRIDROUTE_REQUIRE_REDIS=1 so they fail instead.
"""
import os

import pytest
import redis

from core.scripts import Scripts
from tests.helpers import TEST_REDIS_URL


@pytest.fixture
def r():
    client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        client.ping()
    except redis.ConnectionError as e:
        if os.environ.get("GRIDROUTE_REQUIRE_REDIS"):
            raise
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL} ({e}); run `docker compose up -d redis`")
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


@pytest.fixture
def s(r) -> Scripts:
    return Scripts(r)
