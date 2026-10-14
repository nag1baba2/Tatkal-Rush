"""Single-threaded behaviour of the booking service."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models import Booking
from app.services.booking import (
    IdempotencyKeyReuse,
    SeatNotFound,
    SeatUnavailable,
    create_booking,
)


def book(db, world, *, user="u1", seat="seat1", sched="s1", key="k1", **kw):
    return create_booking(
        db,
        user_id=world[user].id,
        schedule_id=world[sched].id,
        seat_id=world[seat].id,
        idempotency_key=key,
        **kw,
    )


def count_bookings(db):
    return db.scalar(select(func.count()).select_from(Booking))


def test_books_a_free_seat_and_holds_it(db, world):
    before = datetime.now(timezone.utc)
    booking, created = book(db, world)
    assert created is True
    assert booking.status == "HELD"
    expected = before + timedelta(seconds=settings.hold_seconds)
    assert abs((booking.hold_expires_at - expected).total_seconds()) < 5


def test_taken_seat_is_refused(db, world):
    book(db, world, user="u1", key="a")
    with pytest.raises(SeatUnavailable):
        book(db, world, user="u2", key="b")
    assert count_bookings(db) == 1


def test_same_key_replays_the_original_booking(db, world):
    first, created1 = book(db, world, key="same")
    second, created2 = book(db, world, key="same")
    assert (created1, created2) == (True, False)
    assert first.id == second.id
    assert count_bookings(db) == 1


def test_same_key_with_a_different_seat_is_rejected(db, world):
    book(db, world, key="same")
    with pytest.raises(IdempotencyKeyReuse):
        book(db, world, seat="seat2", sched="s2", key="same")
    assert count_bookings(db) == 1


def test_unknown_seat_is_not_found(db, world):
    with pytest.raises(SeatNotFound):
        create_booking(
            db,
            user_id=world["u1"].id,
            schedule_id=world["s1"].id,
            seat_id=0,
            idempotency_key="k",
        )


def test_seat_from_another_schedule_is_not_found(db, world):
    with pytest.raises(SeatNotFound):
        book(db, world, seat="seat1", sched="s2")


def test_expired_hold_is_released_lazily(db, world):
    old, _ = book(db, world, user="u1", key="a", hold_seconds=0)
    new, created = book(db, world, user="u2", key="b")
    assert created is True
    db.refresh(old)
    assert old.status == "EXPIRED"
    assert new.status == "HELD"
    active = db.scalar(
        select(func.count()).select_from(Booking).where(
            Booking.seat_id == world["seat1"].id,
            Booking.status.in_(("HELD", "CONFIRMED")),
        )
    )
    assert active == 1


def test_confirmed_booking_is_never_released_by_the_clock(db, world):
    old, _ = book(db, world, user="u1", key="a", hold_seconds=0)
    old.status = "CONFIRMED"  # paid; hold_expires_at is now irrelevant
    db.commit()
    with pytest.raises(SeatUnavailable):
        book(db, world, user="u2", key="b")
