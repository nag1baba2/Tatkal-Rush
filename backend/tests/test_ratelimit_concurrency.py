"""Why the limiter is a Lua script: under concurrency it admits exactly `capacity`
requests, while a naive read-modify-write version lets extra ones through."""

from collections import Counter
from uuid import uuid4

import redis

from app.config import settings
from app.ratelimit import TokenBucketLimiter
from tests.helpers import run_parallel


def make(capacity, refill=0.001):
    client = redis.Redis.from_url(settings.redis_url)
    prefix = f"rltest:{uuid4().hex}:"
    limiter = TokenBucketLimiter(
        client, capacity=capacity, refill_per_second=refill,
        prefix=prefix, bypass_key=prefix + "bypass",
    )
    return limiter, client, prefix


def cleanup(client, prefix):
    for key in client.scan_iter(match=prefix + "*"):
        client.delete(key)


def test_500_simultaneous_requests_get_exactly_capacity_tokens():
    limiter, client, prefix = make(capacity=10)
    try:
        results = run_parallel(lambda _: limiter.check("bot").allowed, list(range(500)))
        assert results.count(True) == 10  # never 11
        assert results.count(False) == 490
        assert limiter.stats() == {"allowed": 10, "denied": 490}
    finally:
        cleanup(client, prefix)


def test_200_requests_over_20_identities_get_exactly_capacity_each():
    limiter, client, prefix = make(capacity=5)
    try:
        results = run_parallel(lambda i: (i % 20, limiter.check(f"user-{i % 20}").allowed), list(range(200)))
        allowed = Counter(identity for identity, ok in results if ok)
        assert allowed == {i: 5 for i in range(20)}
    finally:
        cleanup(client, prefix)


def naive_check(client, key, capacity):
    """What NOT to do: separate GET and SET, so concurrent requests can all read the
    same token count before any of them writes it back."""
    raw = client.get(key)
    tokens = float(raw) if raw is not None else float(capacity)
    if tokens >= 1:
        client.set(key, tokens - 1)
        return True
    return False


def test_a_naive_read_modify_write_limiter_lets_extra_requests_through():
    """Documents the problem the Lua script solves. It races, so try several rounds;
    over-admission shows up in practically the first one."""
    client = redis.Redis.from_url(settings.redis_url)
    capacity, worst = 10, 0
    for _ in range(10):
        key = f"rltest:naive:{uuid4().hex}"
        try:
            admitted = run_parallel(lambda _: naive_check(client, key, capacity), list(range(200))).count(True)
        finally:
            client.delete(key)
        worst = max(worst, admitted)
        if worst > capacity:
            break
    assert worst > capacity, "expected the naive limiter to over-admit under concurrency"
