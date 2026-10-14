import redis
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.config import settings
from app.db import engine
from app.ratelimit import get_limiters, rate_limit
from app.routers import auth, bookings, payments, schedules, trains
from app.services.payment import default_breaker

app = FastAPI(title="Tatkal-Rush", dependencies=[Depends(rate_limit)])
app.include_router(auth.router)
app.include_router(bookings.router)
app.include_router(payments.router)
app.include_router(schedules.router)
app.include_router(trains.router)

# Let the browser UI (the Vite dev server) call the API. The custom request headers
# must be allowed explicitly, and the response headers the UI reads must be exposed.
# Added last so it is the outermost middleware and also covers error responses.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    expose_headers=["Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining", "Idempotent-Replay"],
)


@app.exception_handler(PoolTimeoutError)
@app.exception_handler(OperationalError)
async def database_overloaded(request: Request, exc: Exception):
    """No DB connection became free in time (pool timeout), or Postgres refused one
    ("too many clients"), or a connection dropped. All are transient overload, so tell the
    client to retry rather than reporting a generic 500; the idempotency key makes the
    retry safe."""
    return JSONResponse(
        status_code=503,
        content={"detail": "server_busy"},
        headers={"Retry-After": "1"},
    )


@app.get("/health")
def health():
    """Reports whether the API can reach Postgres and Redis."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    redis.Redis.from_url(settings.redis_url).ping()
    return {
        "status": "ok",
        "postgres": "up",
        "redis": "up",
        "payment_circuit": default_breaker().state,
        # "closed" = limiter healthy; "open" = Redis unreachable, failing open
        "rate_limiter_circuit": get_limiters().breaker.state,
    }
