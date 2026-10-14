"""Token bucket maths, fail-open behaviour, and the HTTP layer.

The Lua script normally reads the clock from Redis. These tests pass an explicit
`now_ms` so the maths is exact and no test has to sleep (one test uses real time
on purpose, for key expiry).
"""

import time
from uuid import uuid4

import pytest
import redis

from app.auth import create_access_token
from app.circuit_breaker import CircuitBreaker
from app.config import settings
from app.main import app
from app.ratelimit import Limiters, TokenBucketLimiter, get_limiters

T0 = 10_000_000_000  # an arbitrary fake "now", in milliseconds


@pytest.fixture
def redis_client():
    return redis.Redis.from_url(settings.redis_url)


@pytest.fixture
def make_limiter(redis_client):
    prefixes = []

    def make(capacity=5, refill=1.0, **kw):
        prefix = f"rltest:{uuid4().hex}:"
        prefixes.append(prefix)
        kw.setdefault("bypass_key", prefix + "bypass")
        return TokenBucketLimiter(
            kw.pop("client", redis_client), capacity=capacity, refill_per_second=refill,
            prefix=prefix, **kw,
        )

    yield make
    for prefix in prefixes:
        for key in redis_client.scan_iter(match=prefix + "*"):
            redis_client.delete(key)


def drain(limiter, key, n, now=T0):
    return [limiter.check(key, now_ms=now) for _ in range(n)]


# ---- the bucket ------------------------------------------------------------------

def test_a_burst_up_to_capacity_is_allowed_then_refused(make_limiter):
    lim = make_limiter(capacity=5, refill=1.0)
    results = drain(lim, "u", 6)
    assert [r.allowed for r in results] == [True] * 5 + [False]
    assert [round(r.remaining) for r in results[:5]] == [4, 3, 2, 1, 0]


def test_retry_after_says_how_long_until_the_next_token(make_limiter):
    lim = make_limiter(capacity=5, refill=5.0)  # one token every 200 ms
    drain(lim, "u", 5)
    refused = lim.check("u", now_ms=T0)
    assert not refused.allowed
    assert refused.retry_after == pytest.approx(0.2)


def test_tokens_refill_with_time(make_limiter):
    lim = make_limiter(capacity=5, refill=5.0)
    drain(lim, "u", 5)
    after_400ms = T0 + 400  # 400 ms * 5/s = 2 tokens
    assert [lim.check("u", now_ms=after_400ms).allowed for _ in range(3)] == [True, True, False]


def test_partial_tokens_accumulate(make_limiter):
    lim = make_limiter(capacity=2, refill=1.0)
    drain(lim, "u", 2)
    half = lim.check("u", now_ms=T0 + 500)
    assert not half.allowed and half.retry_after == pytest.approx(0.5)
    assert lim.check("u", now_ms=T0 + 1000).allowed  # the two halves add up to one token


def test_an_idle_bucket_never_holds_more_than_its_capacity(make_limiter):
    lim = make_limiter(capacity=3, refill=10.0)
    drain(lim, "u", 3)
    an_hour_later = T0 + 3_600_000
    assert [lim.check("u", now_ms=an_hour_later).allowed for _ in range(4)] == [True] * 3 + [False]


def test_requests_can_cost_more_than_one_token(make_limiter):
    lim = make_limiter(capacity=5, refill=0.001)
    assert lim.check("u", cost=3, now_ms=T0).allowed
    assert not lim.check("u", cost=3, now_ms=T0).allowed
    assert lim.check("u", cost=2, now_ms=T0).allowed


def test_a_cost_above_capacity_is_never_allowed(make_limiter):
    lim = make_limiter(capacity=2, refill=1.0)
    assert not lim.check("u", cost=3, now_ms=T0).allowed


def test_identities_have_independent_buckets(make_limiter):
    lim = make_limiter(capacity=2, refill=0.001)
    drain(lim, "alice", 2)
    assert not lim.check("alice", now_ms=T0).allowed
    assert lim.check("bob", now_ms=T0).allowed


def test_a_clock_that_goes_backwards_does_not_mint_tokens(make_limiter):
    lim = make_limiter(capacity=2, refill=1.0)
    drain(lim, "u", 2, now=T0 + 10_000)
    assert not lim.check("u", now_ms=T0).allowed  # "earlier" than the last request


def test_an_idle_bucket_is_forgotten_by_redis(make_limiter, redis_client):
    lim = make_limiter(capacity=1, refill=20.0)  # full again after 50 ms; kept for 100 ms
    lim.check("u")  # real clock
    key = lim.bucket_key("u")
    assert 0 < redis_client.pttl(key) <= 100
    time.sleep(0.4)
    assert redis_client.exists(key) == 0


def test_stats_count_allowed_and_denied(make_limiter):
    lim = make_limiter(capacity=2, refill=0.001)
    drain(lim, "u", 5)
    assert lim.stats() == {"allowed": 2, "denied": 3}


def test_the_bypass_flag_lets_everything_through_without_consuming(make_limiter, redis_client):
    lim = make_limiter(capacity=1, refill=0.001)
    redis_client.set(lim.bypass_key, "1", ex=60)
    results = drain(lim, "u", 5)
    assert all(r.allowed and r.bypassed for r in results)
    assert redis_client.exists(lim.bucket_key("u")) == 0  # nothing was consumed

    redis_client.delete(lim.bypass_key)
    assert [lim.check("u", now_ms=T0).allowed for _ in range(2)] == [True, False]


@pytest.mark.parametrize("capacity,refill", [(0, 1.0), (-1, 1.0), (5, 0), (5, -1.0)])
def test_nonsense_configuration_is_rejected(redis_client, capacity, refill):
    with pytest.raises(ValueError):
        TokenBucketLimiter(redis_client, capacity=capacity, refill_per_second=refill)


# ---- fail open ---------------------------------------------------------------------

def dead_client():
    return redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2)


def test_an_unreachable_redis_fails_open(make_limiter):
    lim = make_limiter(client=dead_client())
    result = lim.check("u")
    assert result.allowed and result.degraded


def test_the_breaker_stops_every_request_waiting_on_a_dead_redis(make_limiter):
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
    lim = make_limiter(client=dead_client(), breaker=breaker)
    for _ in range(3):
        assert lim.check("u").degraded
    assert breaker.state == "open"

    started = time.perf_counter()
    results = [lim.check("u") for _ in range(200)]
    elapsed = time.perf_counter() - started
    assert all(r.allowed and r.degraded for r in results)
    assert elapsed < 0.5  # no network round trips any more


def test_enforcement_resumes_when_redis_comes_back(make_limiter):
    class Clock:
        t = 0.0

        def __call__(self):
            return self.t

    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=5, clock=clock)
    lim = make_limiter(capacity=1, refill=0.001, breaker=breaker)
    real_script = lim._script
    outage = {"on": True}

    def flaky_script(**kwargs):
        if outage["on"]:
            raise redis.ConnectionError("down")
        return real_script(**kwargs)

    lim._script = flaky_script

    assert lim.check("u").degraded and lim.check("u").degraded  # outage: let through
    assert breaker.state == "open"

    outage["on"] = False
    clock.t = 5.0  # the breaker's probe is due
    first = lim.check("u", now_ms=T0)
    assert first.allowed and not first.degraded  # back to real enforcement
    assert not lim.check("u", now_ms=T0).allowed  # and the bucket is empty again


# ---- the HTTP layer ------------------------------------------------------------------

def install(identity_capacity=3, ip_capacity=100, *, client=None):
    """Replace the app's limiters with ones on an isolated key prefix and a bucket
    that effectively never refills during the test."""
    prefix = f"rltest:{uuid4().hex}:"
    redis_client = client or redis.Redis.from_url(settings.redis_url)
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=2)

    def build(scope, capacity):
        return TokenBucketLimiter(
            redis_client, capacity=capacity, refill_per_second=0.001, scope=scope,
            prefix=prefix, bypass_key=prefix + "bypass", breaker=breaker,
        )

    limiters = Limiters(identity=build("id", identity_capacity), ip=build("ip", ip_capacity), breaker=breaker)
    app.dependency_overrides[get_limiters] = lambda: limiters
    return prefix, redis_client


@pytest.fixture
def limited(client):
    """`client` plus cleanup of the keys a test's limiters created."""
    holder = {}

    def start(**kw):
        holder["prefix"], holder["redis"] = install(**kw)

    yield start
    if holder:
        # Clean up through a healthy connection: a test may have installed a limiter
        # on a deliberately dead Redis client.
        healthy = redis.Redis.from_url(settings.redis_url)
        for key in healthy.scan_iter(match=holder["prefix"] + "*"):
            healthy.delete(key)


def seats(client, world, headers=None):
    return client.get(f"/schedules/{world['s1'].id}/seats", headers=headers or {})


def bearer(world, user):
    return {"Authorization": f"Bearer {create_access_token(world[user].id)}"}


def test_anonymous_requests_are_limited_per_ip_with_helpful_headers(client, world, limited):
    limited(identity_capacity=3)
    ok = [seats(client, world) for _ in range(3)]
    assert [r.status_code for r in ok] == [200, 200, 200]
    assert [r.headers["X-RateLimit-Remaining"] for r in ok] == ["2", "1", "0"]
    assert ok[0].headers["X-RateLimit-Limit"] == "3"

    refused = seats(client, world)
    assert refused.status_code == 429
    assert refused.json()["detail"] == "rate_limited"
    assert int(refused.headers["Retry-After"]) >= 1
    assert refused.headers["X-RateLimit-Remaining"] == "0"


def test_each_logged_in_user_has_their_own_bucket(client, world, limited):
    limited(identity_capacity=3)
    assert [seats(client, world, bearer(world, "u1")).status_code for _ in range(4)] == [200, 200, 200, 429]
    assert seats(client, world, bearer(world, "u2")).status_code == 200  # same IP, different user


def test_flooding_anonymously_does_not_block_a_logged_in_user(client, world, limited):
    limited(identity_capacity=3)
    assert [seats(client, world).status_code for _ in range(4)] == [200, 200, 200, 429]
    assert seats(client, world, bearer(world, "u1")).status_code == 200


def test_the_ip_ceiling_stops_many_accounts_on_one_address(client, world, limited):
    limited(identity_capacity=100, ip_capacity=3)
    codes = [seats(client, world, bearer(world, "u1" if i % 2 else "u2")).status_code for i in range(4)]
    assert codes == [200, 200, 200, 429]  # each user's own bucket was fine; the IP was not


def test_a_forged_or_expired_token_cannot_choose_its_bucket(client, world, limited):
    limited(identity_capacity=3)
    forged = {"Authorization": "Bearer not.a.token"}
    expired = {"Authorization": f"Bearer {create_access_token(world['u1'].id, expires_minutes=-1)}"}
    codes = [seats(client, world, h).status_code for h in (forged, expired, forged, expired)]
    assert codes == [200, 200, 200, 429]  # all of them fell back to the shared IP bucket


def test_health_checks_are_never_limited(client, limited):
    limited(identity_capacity=1, ip_capacity=1)
    assert all(client.get("/health").status_code == 200 for _ in range(10))


def test_x_forwarded_for_is_ignored_unless_trusted(client, world, limited, monkeypatch):
    limited(identity_capacity=3)
    spoofed = [seats(client, world, {"X-Forwarded-For": f"9.9.9.{i}"}).status_code for i in range(4)]
    assert spoofed == [200, 200, 200, 429]  # rotating the header did not buy new buckets


def test_x_forwarded_for_is_used_when_trusted(client, world, limited, monkeypatch):
    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    limited(identity_capacity=2)
    a = [seats(client, world, {"X-Forwarded-For": "1.1.1.1, 10.0.0.1"}).status_code for _ in range(3)]
    b = seats(client, world, {"X-Forwarded-For": "2.2.2.2"}).status_code
    assert a == [200, 200, 429] and b == 200


def test_the_switch_turns_limiting_off(client, world, limited, monkeypatch):
    limited(identity_capacity=1)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    assert all(seats(client, world).status_code == 200 for _ in range(5))


def test_a_refused_request_never_reaches_the_endpoint(client, world, limited):
    from sqlalchemy import func, select

    from app.models import Booking

    limited(identity_capacity=1)
    headers = {**bearer(world, "u1"), "Idempotency-Key": "k1"}
    first = client.post("/bookings", json={"schedule_id": world["s1"].id, "seat_id": world["seat1"].id}, headers=headers)
    second = client.post("/bookings", json={"schedule_id": world["s2"].id, "seat_id": world["seat2"].id},
                         headers={**headers, "Idempotency-Key": "k2"})
    assert (first.status_code, second.status_code) == (201, 429)
    db = app.dependency_overrides[__import__("app.db", fromlist=["get_db"]).get_db]()
    assert db.scalar(select(func.count()).select_from(Booking)) == 1


def test_the_api_fails_open_when_the_limiter_cannot_reach_redis(client, world, limited):
    limited(identity_capacity=1, client=dead_client())
    assert all(seats(client, world).status_code == 200 for _ in range(10))
