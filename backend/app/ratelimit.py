"""Token-bucket rate limiter backed by Redis.

A bucket holds up to `capacity` tokens and refills continuously at
`refill_per_second`. Each request takes one token; with none left the request is
refused (HTTP 429) and told how long to wait. That allows short bursts but caps the
sustained rate.

WHY A LUA SCRIPT: the update is read bucket -> add refill -> take a token -> write
back. Done as separate Redis commands, two simultaneous requests can both read
"1 token left" and both be let through. Redis runs a Lua script start to finish
without interleaving any other command, so the whole step is atomic. The script
also reads the clock from Redis itself (TIME), so every API process shares one
clock and a client with a skewed clock cannot cheat.

FAIL OPEN: if Redis is unreachable the limiter lets requests through (and logs),
because the database already guarantees correctness (no double booking, no double
charge). The limiter only protects capacity; refusing every request because the
limiter's own store is down would turn a small outage into a total one. A circuit
breaker stops each request from waiting on the dead connection.

Two layers per request (see rate_limit): a per-identity bucket (user from the JWT,
or the IP when not logged in) and a larger per-IP ceiling.
"""

import logging
import math
import time
from dataclasses import dataclass
from functools import lru_cache

import redis
from fastapi import Depends, HTTPException, Request, Response

from app.auth import user_id_from_token
from app.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.config import settings

log = logging.getLogger(__name__)

BYPASS_KEY = "tatkal:ratelimit:bypass"
EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

# KEYS: bucket, bypass flag, stats hash.  ARGV: capacity, refill/s, cost, now_ms ('' = use Redis TIME).
# Returns {allowed, tokens_left (string), retry_after_ms, bypassed}.
LUA = """
local bucket = KEYS[1]
local bypass = KEYS[2]
local stats  = KEYS[3]
local capacity = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])
local cost     = tonumber(ARGV[3])

if redis.call('EXISTS', bypass) == 1 then
    redis.call('HINCRBY', stats, 'bypassed', 1)
    return {1, tostring(capacity), 0, 1}
end

local now
if ARGV[4] ~= '' then
    now = tonumber(ARGV[4])
else
    local t = redis.call('TIME')
    now = t[1] * 1000 + math.floor(t[2] / 1000)
end

local data = redis.call('HMGET', bucket, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil or ts == nil then
    tokens = capacity
    ts = now
end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate / 1000)

local allowed = 0
local retry_ms = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
    redis.call('HINCRBY', stats, 'allowed', 1)
else
    retry_ms = math.ceil((cost - tokens) * 1000 / rate)
    redis.call('HINCRBY', stats, 'denied', 1)
end

redis.call('HSET', bucket, 'tokens', string.format('%.6f', tokens), 'ts', string.format('%d', now))
-- An idle bucket is full again after capacity/rate seconds; keep it twice that long, then forget it.
redis.call('PEXPIRE', bucket, math.ceil(capacity / rate * 1000) * 2)
return {allowed, string.format('%.6f', tokens), retry_ms, 0}
"""


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: float
    retry_after: float = 0.0  # seconds until a request would be allowed
    degraded: bool = False    # the limiter was unavailable, so the request was let through
    bypassed: bool = False    # the ops bypass flag is set


class TokenBucketLimiter:
    def __init__(
        self,
        client: redis.Redis,
        *,
        capacity: int,
        refill_per_second: float,
        scope: str = "id",
        prefix: str = "rl:",
        bypass_key: str = BYPASS_KEY,
        breaker: CircuitBreaker | None = None,
    ):
        if capacity < 1 or refill_per_second <= 0:
            raise ValueError("capacity must be >= 1 and refill_per_second > 0")
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.scope = scope
        self.prefix = prefix
        self.bypass_key = bypass_key
        self.stats_key = prefix + "stats"
        self.breaker = breaker
        self._client = client
        self._script = client.register_script(LUA)
        self._last_warning = 0.0

    def bucket_key(self, identity: str) -> str:
        return f"{self.prefix}bucket:{self.scope}:{identity}"

    def check(self, identity: str, cost: int = 1, now_ms: int | None = None) -> RateLimitResult:
        """Take `cost` tokens from this identity's bucket if it has them."""
        kwargs = dict(
            keys=[self.bucket_key(identity), self.bypass_key, self.stats_key],
            args=[self.capacity, self.refill_per_second, cost, "" if now_ms is None else now_ms],
        )
        try:
            if self.breaker is not None:
                raw = self.breaker.call(self._script, **kwargs)
            else:
                raw = self._script(**kwargs)
        except CircuitOpenError:
            return self._fail_open(log_it=False)
        except redis.RedisError:
            return self._fail_open(log_it=True)

        allowed, tokens, retry_ms, bypassed = int(raw[0]), float(raw[1]), int(raw[2]), int(raw[3])
        return RateLimitResult(
            allowed=bool(allowed),
            limit=self.capacity,
            remaining=tokens,
            retry_after=retry_ms / 1000,
            bypassed=bool(bypassed),
        )

    def _fail_open(self, log_it: bool) -> RateLimitResult:
        now = time.monotonic()
        if log_it and now - self._last_warning > 5:
            self._last_warning = now
            log.warning("rate limiter unavailable (Redis); failing open")
        return RateLimitResult(allowed=True, limit=self.capacity, remaining=self.capacity, degraded=True)

    def stats(self) -> dict[str, int]:
        return {k.decode(): int(v) for k, v in self._client.hgetall(self.stats_key).items()}


@dataclass
class Limiters:
    identity: TokenBucketLimiter
    ip: TokenBucketLimiter
    breaker: CircuitBreaker


@lru_cache
def get_limiters() -> Limiters:
    # Short timeouts: a hung Redis must not stall every request.
    client = redis.Redis.from_url(
        settings.redis_url, socket_timeout=0.25, socket_connect_timeout=0.25
    )
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=2.0)
    return Limiters(
        identity=TokenBucketLimiter(
            client, capacity=settings.rate_limit_capacity,
            refill_per_second=settings.rate_limit_refill_per_second,
            scope="id", breaker=breaker,
        ),
        ip=TokenBucketLimiter(
            client, capacity=settings.rate_limit_ip_capacity,
            refill_per_second=settings.rate_limit_ip_refill_per_second,
            scope="ip", breaker=breaker,
        ),
        breaker=breaker,
    )


def set_bypass(ttl_seconds: int = 900) -> None:
    """Ops switch: while set, nothing is rate limited (e.g. correctness load tests).
    Always has a TTL so it cannot be left on by accident."""
    redis.Redis.from_url(settings.redis_url).set(BYPASS_KEY, "1", ex=ttl_seconds)


def clear_bypass() -> None:
    redis.Redis.from_url(settings.redis_url).delete(BYPASS_KEY)


def client_ip(request: Request) -> str:
    if settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def identity_for(request: Request) -> str:
    """The logged-in user if the bearer token verifies, else the client IP.
    A forged or expired token cannot pick a bucket: it falls back to the IP."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        user_id = user_id_from_token(header[7:].strip())
        if user_id is not None:
            return f"user:{user_id}"
    return f"anon:{client_ip(request)}"


def _too_many(result: RateLimitResult) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="rate_limited",
        headers={
            "Retry-After": str(max(1, math.ceil(result.retry_after))),
            "X-RateLimit-Limit": str(result.limit),
            "X-RateLimit-Remaining": "0",
        },
    )


def rate_limit(
    request: Request, response: Response, limiters: Limiters = Depends(get_limiters)
) -> None:
    """Global dependency. Checks the broad per-IP ceiling first, then the per-identity
    bucket. Touches only Redis and the JWT signature (never the database)."""
    if not settings.rate_limit_enabled or request.url.path in EXEMPT_PATHS:
        return

    ceiling = limiters.ip.check(client_ip(request))
    if not ceiling.allowed:
        raise _too_many(ceiling)
    mine = limiters.identity.check(identity_for(request))
    if not mine.allowed:
        raise _too_many(mine)

    response.headers["X-RateLimit-Limit"] = str(mine.limit)
    response.headers["X-RateLimit-Remaining"] = str(int(min(mine.remaining, ceiling.remaining)))
