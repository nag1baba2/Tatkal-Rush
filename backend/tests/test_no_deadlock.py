"""Regression tests for two defects found by the 1,000-user load test.

1. THREADS-VS-CONNECTIONS DEADLOCK. FastAPI runs synchronous endpoints on a pool of
   worker threads. A dependency whose cleanup also runs on that pool (the old
   synchronous get_db) cannot return a database connection until a thread is free,
   while every thread is waiting for a connection: a stall that only the pool timeout
   breaks. These tests shrink the thread pool and the connection pool to 2 each so
   the stall shows up immediately.

2. LAZY LOADS DURING SERIALISATION. rollback() expires every loaded object, and a
   server-side `onupdate` leaves updated_at unloaded, so building the HTTP response
   silently ran extra queries (and needed a connection) inside a worker thread.
   Everything the response schemas read must already be loaded when the service returns.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import app.db
from app.auth import create_access_token
from app.circuit_breaker import CircuitBreaker
from app.config import settings
from app.gateway import GatewayDeclined, GatewayTimeout
from app.main import app as fastapi_app
from app.schemas import BookingOut, PaymentOut
from app.services.booking import create_booking
from app.services.payment import pay_booking
from tests.helpers import ScriptedGateway, committed_world, hold, no_sleep, pay

PAYMENT_FIELDS = list(PaymentOut.model_fields)
BOOKING_FIELDS = list(BookingOut.model_fields)


def unloaded(obj, fields):
    """Which of these attributes would trigger a database query if read now?"""
    return sorted(set(inspect(obj).unloaded) & set(fields))


# ---- 1. the deadlock -------------------------------------------------------------------

@pytest.fixture
def tiny_pools(monkeypatch):
    """2 connections and 2 worker threads: easy to exhaust both."""
    engine = create_engine(settings.database_url, pool_size=2, max_overflow=0, pool_timeout=2)
    monkeypatch.setattr(app.db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    yield
    engine.dispose()


def test_read_endpoints_survive_running_out_of_threads_and_connections(tiny_pools):
    with committed_world(n_users=1, n_seats=3) as w, TestClient(fastapi_app) as client:

        async def shrink_thread_pool():
            anyio.to_thread.current_default_thread_limiter().total_tokens = 2

        client.portal.call(shrink_thread_pool)
        auth = {"Authorization": f"Bearer {create_access_token(w.user_ids[0])}"}
        # Every read path: seat map, train search, schedules (with availability), and the
        # authenticated "my bookings". Each must end its transaction before returning.
        requests = [
            (f"/schedules/{w.schedule_id}/seats", {}),
            ("/trains?source=A", {}),
            ("/trains/%d/schedules" % w.train_id, {}),
            ("/bookings", auth),
        ] * 15

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=24) as pool:
            statuses = list(pool.map(lambda r: client.get(r[0], headers=r[1]).status_code, requests))
        elapsed = time.monotonic() - started

        assert statuses.count(200) == len(requests), f"statuses: {sorted(set(statuses))}"
        # With the deadlock every wave waits for the 6 s pool timeout; healthy takes well under 5 s.
        assert elapsed < 5, f"{len(requests)} requests took {elapsed:.1f}s: the connection pool is stalling"


# ---- 2. no hidden queries while building a response ------------------------------------------------

def test_a_replayed_booking_is_fully_loaded(db, world):
    first, _ = create_booking(db, user_id=world["u1"].id, schedule_id=world["s1"].id,
                              seat_id=world["seat1"].id, idempotency_key="k")
    replay, created = create_booking(db, user_id=world["u1"].id, schedule_id=world["s1"].id,
                                     seat_id=world["seat1"].id, idempotency_key="k")
    assert created is False
    assert unloaded(replay, BOOKING_FIELDS) == []


def test_a_new_booking_is_fully_loaded(db, world):
    booking = hold(db, world)
    assert unloaded(booking, BOOKING_FIELDS) == []


def _payment_outcomes(db, world):
    """A payment in each state the API can return, plus replays of them."""
    booking = hold(db, world)
    paid = pay(db, world, booking, ScriptedGateway("gw_1"), key="p1")
    yield "success", paid.payment
    yield "success replay", pay(db, world, booking, ScriptedGateway(), key="p1").payment

    other = hold(db, world, seat="seat2", sched="s2", key="b2")
    declined = pay(db, world, other, ScriptedGateway(GatewayDeclined()), key="p2")
    yield "declined", declined.payment
    yield "declined replay", pay(db, world, other, ScriptedGateway(), key="p2").payment


def test_every_payment_outcome_is_fully_loaded(db, world):
    for label, payment in _payment_outcomes(db, world):
        assert unloaded(payment, PAYMENT_FIELDS) == [], f"{label}: would query the database when serialised"


def test_a_pending_payment_and_its_replay_are_fully_loaded(db, world):
    booking = hold(db, world)
    first = pay(db, world, booking, ScriptedGateway(default=GatewayTimeout()))
    again = pay(db, world, booking, ScriptedGateway())
    assert first.payment.status == "PENDING"
    assert unloaded(first.payment, PAYMENT_FIELDS) == []
    assert unloaded(again.payment, PAYMENT_FIELDS) == []


def _selects(db):
    from sqlalchemy import event

    statements = []
    event.listen(db.get_bind(), "before_cursor_execute",
                 lambda conn, cur, statement, *a: statements.append(statement) if statement.lstrip().upper().startswith("SELECT") else None)
    return statements


def test_replaying_a_booking_runs_exactly_one_query(db, world):
    """rollback() followed by reading an attribute silently opens a NEW transaction that
    nobody closes. A replay must look the booking up once and be done."""
    create_booking(db, user_id=world["u1"].id, schedule_id=world["s1"].id,
                   seat_id=world["seat1"].id, idempotency_key="k")
    selects = _selects(db)
    create_booking(db, user_id=world["u1"].id, schedule_id=world["s1"].id,
                   seat_id=world["seat1"].id, idempotency_key="k")
    assert len(selects) == 1, selects


def test_replaying_a_payment_runs_no_query_beyond_its_two_lookups(db, world):
    booking = hold(db, world)
    pay(db, world, booking, ScriptedGateway("gw_1"), key="p1")
    selects = _selects(db)
    pay(db, world, booking, ScriptedGateway(), key="p1")   # a replay
    assert len(selects) == 2, selects   # the booking and the payment, each once


def test_serialising_a_payment_response_makes_no_queries(db, world):
    from sqlalchemy import event

    booking = hold(db, world)
    result = pay(db, world, booking, ScriptedGateway("gw_1"))
    statements = []
    event.listen(db.get_bind(), "before_cursor_execute", lambda *a: statements.append(a[2]))
    PaymentOut.model_validate(result.payment)
    assert statements == []


def test_error_responses_do_not_stall_the_pool_either(tiny_pools):
    """A 404 is raised while the request's transaction is still open, so the connection
    is only returned by the dependency's cleanup. That cleanup must not need a worker
    thread (hence get_db is an async generator)."""
    with TestClient(fastapi_app) as client:

        async def shrink_thread_pool():
            anyio.to_thread.current_default_thread_limiter().total_tokens = 2

        client.portal.call(shrink_thread_pool)
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=24) as pool:
            statuses = list(pool.map(lambda _: client.get("/schedules/0/seats").status_code, range(80)))
        elapsed = time.monotonic() - started

        assert statuses.count(404) == 80, f"statuses: {sorted(set(statuses))}"
        assert elapsed < 5, f"80 requests took {elapsed:.1f}s: the connection pool is stalling"
