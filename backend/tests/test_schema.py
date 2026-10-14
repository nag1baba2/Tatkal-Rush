"""Proves the database itself rejects bad data, independent of application code.

These run against real Postgres (not SQLite) because partial unique indexes and
CHECK constraints behave differently there.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Booking, Payment

FUTURE = datetime.now(timezone.utc) + timedelta(minutes=5)


def booking(world, *, user="u1", seat="seat1", sched="s1", status="HELD",
            key="k1", expires=FUTURE):
    return Booking(
        user_id=world[user].id,
        seat_id=world[seat].id,
        schedule_id=world[sched].id,
        status=status,
        hold_expires_at=expires if status == "HELD" else None,
        idempotency_key=key,
    )


def insert_fails(db, obj):
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.add(obj)
            db.flush()


def test_second_active_booking_for_same_seat_is_rejected(db, world):
    db.add(booking(world, user="u1", key="a"))
    db.flush()
    insert_fails(db, booking(world, user="u2", key="b"))


def test_held_then_confirmed_still_blocks_other_users(db, world):
    db.add(booking(world, user="u1", status="CONFIRMED", key="a"))
    db.flush()
    insert_fails(db, booking(world, user="u2", key="b"))


def test_seat_can_be_rebooked_after_hold_expires(db, world):
    db.add(booking(world, user="u1", status="EXPIRED", key="a"))
    db.add(booking(world, user="u2", status="CANCELLED", key="b"))
    db.add(booking(world, user="u2", status="HELD", key="c"))
    db.flush()  # no IntegrityError: only one ACTIVE row exists


def test_same_user_and_idempotency_key_is_rejected(db, world):
    db.add(booking(world, user="u1", key="same"))
    db.flush()
    insert_fails(db, booking(world, user="u1", seat="seat2", sched="s2", key="same"))


def test_same_idempotency_key_for_different_users_is_allowed(db, world):
    db.add(booking(world, user="u1", key="same"))
    db.add(booking(world, user="u2", seat="seat2", sched="s2", key="same"))
    db.flush()


def test_booking_cannot_pair_seat_with_wrong_schedule(db, world):
    bad = booking(world, seat="seat1", sched="s2", key="x")  # seat1 is in s1
    insert_fails(db, bad)


def test_invalid_booking_status_is_rejected(db, world):
    insert_fails(db, booking(world, status="BOGUS", key="x"))


def test_held_booking_requires_expiry(db, world):
    b = booking(world, key="x")
    b.hold_expires_at = None  # HELD with no expiry must be rejected
    insert_fails(db, b)


def test_one_payment_per_booking(db, world):
    b = booking(world, key="a")
    db.add(b)
    db.flush()
    db.add(Payment(booking_id=b.id, status="CREATED", amount_paise=100,
                   idempotency_key="p1"))
    db.flush()
    insert_fails(db, Payment(booking_id=b.id, status="CREATED", amount_paise=100,
                             idempotency_key="p2"))


def test_payment_idempotency_key_is_unique(db, world):
    b1 = booking(world, key="a")
    b2 = booking(world, user="u2", seat="seat2", sched="s2", key="b")
    db.add_all([b1, b2])
    db.flush()
    db.add(Payment(booking_id=b1.id, status="CREATED", amount_paise=100,
                   idempotency_key="dup"))
    db.flush()
    insert_fails(db, Payment(booking_id=b2.id, status="CREATED", amount_paise=100,
                             idempotency_key="dup"))


def test_payment_rejects_bad_status_and_non_positive_amount(db, world):
    b = booking(world, key="a")
    db.add(b)
    db.flush()
    insert_fails(db, Payment(booking_id=b.id, status="WEIRD", amount_paise=100,
                             idempotency_key="p1"))
    insert_fails(db, Payment(booking_id=b.id, status="CREATED", amount_paise=0,
                             idempotency_key="p2"))
