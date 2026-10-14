from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

# Pool sizes are per API process and configurable: with several uvicorn workers the sum
# must stay below Postgres' max_connections (see app/config.py).
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,   # a saturated pool answers 503 instead of hanging
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


async def get_db():
    """One session per request.

    THE RULE THAT MATTERS (found by the 1,000-user load test): never hold a database
    connection while a request waits for a worker thread. FastAPI runs sync endpoints on a
    40-thread pool and hops back onto it to build the response; a request that still has
    its transaction open at that point holds a connection while it waits for a thread, and
    under load every thread ends up waiting for a connection that is held by a request
    waiting for a thread. So every endpoint ends its transaction (commit) BEFORE it
    returns. tests/test_no_deadlock.py shrinks both pools to 2 to prove this.

    This dependency is an async generator so its cleanup runs on the event loop instead
    of needing a worker thread. That was added on a hypothesis: no test shows it is
    necessary (the stall also does not return when it is a plain generator again), so it
    is defence in depth for error paths, where an endpoint raises with a transaction
    still open. SessionLocal() is cheap (no connection yet) and close() is a quick rollback.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
