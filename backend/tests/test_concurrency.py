"""Real concurrency tests: many simultaneous requests, separate connections.

These run against the live Postgres (and, for the HTTP tests, the live API in
this container). Test data is committed, then deleted in a `finally` block.

Note on honesty: "200 parallel" means 200 requests are released at the same
instant. Database concurrency is bounded by the connection pool (service tests
use 50 connections; the live API uses 20 + 20 overflow), so some requests wait
for a connection, as they would in production.
"""

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import create_access_token
from app.db import SessionLocal
from app.models import Booking, Seat
from app.services.booking import SeatUnavailable, create_booking
from tests.helpers import active_booking_count, committed_world, run_parallel

API_URL = "http://localhost:8000"


def _api_is_up() -> bool:
    try:
        return httpx.get(f"{API_URL}/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


needs_api = pytest.mark.skipif(not _api_is_up(), reason="live API not reachable")


# ---- service level -------------------------------------------------------

def test_200_parallel_bookings_for_one_seat_give_exactly_one_success(pool_sessions):
    with committed_world(n_users=200, n_seats=1) as w:

        def attempt(user_id):
            with pool_sessions() as s:
                try:
                    create_booking(
                        s,
                        user_id=user_id,
                        schedule_id=w.schedule_id,
                        seat_id=w.seat_ids[0],
                        idempotency_key=f"key-{user_id}",
                    )
                    return "booked"
                except SeatUnavailable:
                    return "taken"

        results = run_parallel(attempt, w.user_ids)

        assert results.count("booked") == 1
        assert results.count("taken") == 199
        assert active_booking_count(w.seat_ids[0]) == 1


def test_200_users_across_20_seats_book_each_seat_exactly_once(pool_sessions):
    with committed_world(n_users=200, n_seats=20) as w:

        def attempt(i):
            with pool_sessions() as s:
                try:
                    create_booking(
                        s,
                        user_id=w.user_ids[i],
                        schedule_id=w.schedule_id,
                        seat_id=w.seat_ids[i % 20],  # 10 contenders per seat
                        idempotency_key=f"key-{i}",
                    )
                    return "booked"
                except SeatUnavailable:
                    return "taken"

        results = run_parallel(attempt, list(range(200)))

        assert results.count("booked") == 20
        assert results.count("taken") == 180
        assert all(active_booking_count(seat_id) == 1 for seat_id in w.seat_ids)


def test_booking_really_waits_on_the_seat_row_lock(pool_sessions):
    """Proves the service takes the row lock (not just relying on the index):
    while another transaction holds the seat's lock, a booking must block.

    The holder uses FOR NO KEY UPDATE on purpose. A plain FOR UPDATE would also
    block the booking INSERT through its foreign key to seats (the FK check
    takes FOR KEY SHARE), which would make this test pass even if the service
    forgot its own lock. FOR NO KEY UPDATE conflicts with the service's
    FOR UPDATE but not with the FK check, so only the service's lock can block.
    """
    with committed_world(n_users=1, n_seats=1) as w:
        holder = SessionLocal()
        holder.execute(
            select(Seat)
            .where(Seat.id == w.seat_ids[0])
            .with_for_update(key_share=True)
        )

        done = threading.Event()

        def attempt():
            with pool_sessions() as s:
                create_booking(
                    s,
                    user_id=w.user_ids[0],
                    schedule_id=w.schedule_id,
                    seat_id=w.seat_ids[0],
                    idempotency_key="k",
                )
            done.set()

        t = threading.Thread(target=attempt)
        t.start()
        time.sleep(1.0)
        blocked_while_locked = not done.is_set()

        holder.rollback()  # release the lock
        holder.close()
        t.join(timeout=15)

        assert blocked_while_locked, "booking finished while the seat row was locked"
        assert done.is_set(), "booking never completed after the lock was released"


def test_database_index_alone_stops_double_booking(pool_sessions):
    """Bypass the service entirely: 30 raw concurrent inserts for one seat.
    Only the partial unique index stands in the way, and it must be enough."""
    with committed_world(n_users=30, n_seats=1) as w:

        def raw_insert(i):
            with pool_sessions() as s:
                s.add(
                    Booking(
                        user_id=w.user_ids[i],
                        seat_id=w.seat_ids[0],
                        schedule_id=w.schedule_id,
                        status="HELD",
                        hold_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                        idempotency_key=f"raw-{i}",
                    )
                )
                try:
                    s.commit()
                    return "inserted"
                except IntegrityError as e:
                    s.rollback()
                    return e.orig.diag.constraint_name

        results = run_parallel(raw_insert, list(range(30)))

        assert results.count("inserted") == 1
        assert results.count("uq_bookings_active_seat") == 29
        assert active_booking_count(w.seat_ids[0]) == 1


# ---- HTTP level (live API) ----------------------------------------------

async def _post_many(requests):
    limits = httpx.Limits(max_connections=len(requests))
    async with httpx.AsyncClient(base_url=API_URL, limits=limits, timeout=120) as client:
        responses = await asyncio.gather(
            *[client.post("/bookings", json=body, headers=headers) for body, headers in requests]
        )
    return responses


@needs_api
def test_200_parallel_http_requests_for_one_seat_give_exactly_one_201():
    with committed_world(n_users=200, n_seats=1) as w:
        requests = [
            (
                {"schedule_id": w.schedule_id, "seat_id": w.seat_ids[0]},
                {
                    "Authorization": f"Bearer {create_access_token(uid)}",
                    "Idempotency-Key": f"http-{uid}",
                },
            )
            for uid in w.user_ids
        ]
        responses = asyncio.run(_post_many(requests))
        codes = [r.status_code for r in responses]

        assert codes.count(201) == 1, codes
        assert codes.count(409) == 199, codes
        assert active_booking_count(w.seat_ids[0]) == 1


@needs_api
def test_50_parallel_retries_with_the_same_key_create_one_booking():
    with committed_world(n_users=1, n_seats=1) as w:
        uid = w.user_ids[0]
        request = (
            {"schedule_id": w.schedule_id, "seat_id": w.seat_ids[0]},
            {
                "Authorization": f"Bearer {create_access_token(uid)}",
                "Idempotency-Key": "double-click",
            },
        )
        responses = asyncio.run(_post_many([request] * 50))
        codes = [r.status_code for r in responses]
        ids = {r.json()["id"] for r in responses}

        assert codes.count(201) == 1, codes
        assert codes.count(200) == 49, codes
        assert len(ids) == 1  # every retry got the same booking back
        with SessionLocal() as s:
            rows = s.scalars(select(Booking).where(Booking.user_id == uid)).all()
        assert len(rows) == 1
