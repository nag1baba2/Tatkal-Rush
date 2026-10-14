from datetime import date, datetime, timezone

from uuid import uuid4

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db import engine, get_db
from app.gateway import FakeGateway
from app.main import app
from app.ratelimit import clear_bypass, set_bypass
from app.models import Schedule, Seat, Train, User
from app.worker.control import pause_sweeps, resume_sweeps


@pytest.fixture
def db():
    """A session wrapped in an outer transaction that is always rolled back,
    so tests never leave data behind. commit() only releases a savepoint and
    rollback() only reverts to the last commit."""
    connection = engine.connect()
    outer = connection.begin()
    # expire_on_commit=False mirrors app.db.SessionLocal: tests must see what production sees.
    session = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture
def world(db):
    """Two users, one train, two schedules with one seat each."""
    u1 = User(email="a@example.com", password_hash="x")
    u2 = User(email="b@example.com", password_hash="x")
    train = Train(number="T1", name="Test Express", source="A", destination="B")
    db.add_all([u1, u2, train])
    db.flush()

    def make_schedule(day: int) -> Schedule:
        s = Schedule(
            train_id=train.id,
            travel_date=date(2030, 1, day),
            departs_at=datetime(2030, 1, day, 10, 0, tzinfo=timezone.utc),
            fare_paise=50000,
        )
        db.add(s)
        db.flush()
        return s

    s1, s2 = make_schedule(1), make_schedule(2)
    seat1 = Seat(schedule_id=s1.id, coach="S1", seat_no=1)
    seat2 = Seat(schedule_id=s2.id, coach="S1", seat_no=1)
    db.add_all([seat1, seat2])
    db.flush()
    # Commit so that a later rollback inside the code under test reverts only
    # that code's work, not this setup.
    db.commit()
    return {"u1": u1, "u2": u2, "s1": s1, "s2": s2, "seat1": seat1, "seat2": seat2}


@pytest.fixture
def client(db):
    """In-process API client whose requests use the rolled-back test session."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def pool_sessions():
    """Session factory with its own 50-connection pool for concurrency tests."""
    pool_engine = create_engine(
        settings.database_url, pool_size=50, max_overflow=0, pool_timeout=120
    )
    yield sessionmaker(bind=pool_engine, expire_on_commit=False)
    pool_engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def background_sweeps_paused():
    """The live worker shares this database. Pause its sweeps for the whole run so
    it cannot touch data that concurrency tests count exactly. The flag has a TTL,
    so a crashed run cannot leave the sweeps paused for ever."""
    pause_sweeps(ttl_seconds=1800)
    yield
    resume_sweeps()


@pytest.fixture
def make_gateway():
    """Build FakeGateways on an isolated Redis key prefix; cleaned up afterwards.
    Pass prefix= to share one ledger between two gateways."""
    created = []

    def make(prefix=None, **kw):
        client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        gw = FakeGateway(client, prefix=prefix or f"gwtest:{uuid4().hex}:", **kw)
        created.append(gw)
        return gw

    yield make
    for gw in created:
        gw.reset()


@pytest.fixture(scope="session", autouse=True)
def rate_limiting_bypassed():
    """The live API (used by the HTTP concurrency tests) rate limits by client IP,
    and these tests send hundreds of requests from one address. Bypass it for the
    run; the rate limiter's own tests use limiters with their own bypass key, so
    they are unaffected. The flag has a TTL, so a crashed run cannot leave it on."""
    set_bypass(ttl_seconds=1800)
    yield
    clear_bypass()
