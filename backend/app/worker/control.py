"""A kill switch for the background sweeps, stored in Redis.

While the flag is set the sweep tasks do nothing. Operators can use it to freeze
the sweeps during an incident; the test suite uses it so a live worker cannot
touch the data that concurrency tests count exactly.

The flag always has a TTL, so a forgotten pause cannot disable the sweeps forever.
"""

import redis

from app.config import settings

PAUSE_KEY = "tatkal:sweeps:paused"


def _client() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url)


def pause_sweeps(ttl_seconds: int = 900) -> None:
    _client().set(PAUSE_KEY, "1", ex=ttl_seconds)


def resume_sweeps() -> None:
    _client().delete(PAUSE_KEY)


def sweeps_paused() -> bool:
    return _client().exists(PAUSE_KEY) == 1
