"""The payment state machine, checked twice: in Python and by the Postgres trigger."""

from datetime import datetime, timedelta, timezone
from itertools import product

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import Booking, Payment
from app.services.payment_state import ALLOWED_TRANSITIONS, IllegalTransition, transition

STATUSES = ["CREATED", "PENDING", "SUCCESS", "FAILED"]
ALL_PAIRS = [(a, b) for a, b in product(STATUSES, STATUSES) if a != b]
LEGAL = [p for p in ALL_PAIRS if p in ALLOWED_TRANSITIONS]
ILLEGAL = [p for p in ALL_PAIRS if p not in ALLOWED_TRANSITIONS]

# How to walk a fresh payment (starting at CREATED) to each state.
PATH_TO = {
    "CREATED": [],
    "PENDING": ["PENDING"],
    "SUCCESS": ["PENDING", "SUCCESS"],
    "FAILED": ["PENDING", "FAILED"],
}


class Fake:
    def __init__(self, status):
        self.status = status


@pytest.mark.parametrize("old,new", LEGAL)
def test_python_allows_legal_transitions(old, new):
    p = Fake(old)
    transition(p, new)
    assert p.status == new


@pytest.mark.parametrize("old,new", ILLEGAL + [(s, s) for s in STATUSES])
def test_python_rejects_everything_else(old, new):
    p = Fake(old)
    with pytest.raises(IllegalTransition):
        transition(p, new)
    assert p.status == old  # unchanged


def test_there_are_exactly_four_legal_transitions():
    assert len(LEGAL) == 4


# ---- the database trigger ------------------------------------------------

@pytest.fixture
def payment(db, world):
    booking = Booking(
        user_id=world["u1"].id,
        seat_id=world["seat1"].id,
        schedule_id=world["s1"].id,
        status="HELD",
        hold_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        idempotency_key="k",
    )
    db.add(booking)
    db.flush()
    p = Payment(booking_id=booking.id, status="CREATED", amount_paise=100,
                idempotency_key="p")
    db.add(p)
    db.flush()
    db.commit()
    return p


def set_status(db, payment_id, status):
    with db.begin_nested():
        db.execute(
            text("UPDATE payments SET status = :s WHERE id = :i"),
            {"s": status, "i": payment_id},
        )


def walk_to(db, payment, status):
    for step in PATH_TO[status]:
        set_status(db, payment.id, step)


@pytest.mark.parametrize("old,new", LEGAL)
def test_database_allows_legal_transitions(db, payment, old, new):
    walk_to(db, payment, old)
    set_status(db, payment.id, new)  # must not raise


@pytest.mark.parametrize("old,new", ILLEGAL)
def test_database_rejects_illegal_transitions(db, payment, old, new):
    walk_to(db, payment, old)
    with pytest.raises(IntegrityError, match="illegal payment status transition"):
        set_status(db, payment.id, new)


def test_database_rejects_a_payment_that_does_not_start_as_created(db, world):
    booking = Booking(
        user_id=world["u1"].id,
        seat_id=world["seat1"].id,
        schedule_id=world["s1"].id,
        status="CONFIRMED",
        hold_expires_at=None,
        idempotency_key="k2",
    )
    db.add(booking)
    db.flush()
    with pytest.raises(IntegrityError, match="must start as CREATED"):
        with db.begin_nested():
            db.add(Payment(booking_id=booking.id, status="SUCCESS",
                           amount_paise=100, idempotency_key="p2"))
            db.flush()
