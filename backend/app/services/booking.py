"""Seat booking: the correctness-critical code path.

Three independent layers keep a seat from being double-booked:

1. A row lock (SELECT ... FOR UPDATE) on the seat serialises competing requests,
   so they queue instead of racing.
2. The re-check under the lock sees the winner's committed booking and loses
   cleanly with SeatUnavailable.
3. The partial unique index uq_bookings_active_seat makes the database itself
   refuse a second active booking, even if layers 1 and 2 had a bug.

This relies on Postgres' default READ COMMITTED isolation: after the lock is
acquired, the next SELECT sees rows committed by the previous lock holder.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ACTIVE_BOOKING_STATUSES, Booking, Payment, Seat


class BookingError(Exception):
    """Base class for expected booking failures."""


class SeatNotFound(BookingError):
    pass


class SeatUnavailable(BookingError):
    pass


class IdempotencyKeyReuse(BookingError):
    """The same key was sent with a different seat/schedule."""


def _find_by_key(db: Session, user_id: int, key: str) -> Booking | None:
    return db.scalar(
        select(Booking).where(
            Booking.user_id == user_id, Booking.idempotency_key == key
        )
    )


def _payment_in_flight(db: Session, booking_id: int) -> bool:
    return (
        db.scalar(
            select(Payment.id).where(
                Payment.booking_id == booking_id, Payment.status == "PENDING"
            )
        )
        is not None
    )


def _replay(existing: Booking, schedule_id: int, seat_id: int) -> Booking:
    if existing.seat_id != seat_id or existing.schedule_id != schedule_id:
        raise IdempotencyKeyReuse()
    return existing


def create_booking(
    db: Session,
    *,
    user_id: int,
    schedule_id: int,
    seat_id: int,
    idempotency_key: str,
    hold_seconds: int | None = None,
) -> tuple[Booking, bool]:
    """Hold a seat for a user. Returns (booking, created).

    created=False means the idempotency key was seen before and the original
    booking is returned unchanged.
    """
    hold = settings.hold_seconds if hold_seconds is None else hold_seconds

    # Fast path for retries: no lock needed to look up an existing key.
    existing = _find_by_key(db, user_id, idempotency_key)
    if existing is not None:
        db.commit()  # end the transaction WITHOUT expiring the row we return
        return _replay(existing, schedule_id, seat_id), False

    try:
        # Layer 1: take the row lock. Other requests for this seat wait here
        # until we commit or roll back.
        seat = db.scalar(
            select(Seat)
            .where(Seat.id == seat_id, Seat.schedule_id == schedule_id)
            .with_for_update()
        )
        if seat is None:
            db.rollback()
            raise SeatNotFound()

        # Two identical requests can both pass the fast path; re-check now that
        # we are alone on this seat.
        existing = _find_by_key(db, user_id, idempotency_key)
        if existing is not None:
            db.commit()  # end the transaction WITHOUT expiring the row we return
            return _replay(existing, schedule_id, seat_id), False

        # Layer 2: is the seat already taken?
        now = datetime.now(timezone.utc)
        # FOR UPDATE on the booking too: a payment attempt takes the booking
        # lock before moving to PENDING, so the "is a payment in flight?" check
        # below cannot race with it. Lock order is seat -> booking -> payment.
        active = db.scalar(
            select(Booking)
            .where(
                Booking.seat_id == seat.id,
                Booking.status.in_(ACTIVE_BOOKING_STATUSES),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if active is not None:
            if (
                active.status == "HELD"
                and active.hold_expires_at <= now
                and not _payment_in_flight(db, active.id)
            ):
                # Lazy release: correctness must not depend on the background
                # worker (Phase 4) having run. A hold with a PENDING payment is
                # never released: the money may be moving.
                active.status = "EXPIRED"
                db.flush()
            else:
                db.rollback()
                raise SeatUnavailable()

        booking = Booking(
            user_id=user_id,
            seat_id=seat.id,
            schedule_id=schedule_id,
            status="HELD",
            hold_expires_at=now + timedelta(seconds=hold),
            idempotency_key=idempotency_key,
        )
        db.add(booking)
        db.flush()
        db.commit()  # releases the row lock
        return booking, True

    except IntegrityError:
        # Layer 3: the database constraint caught something the lock should
        # already have prevented. Treat it as "lost the race".
        db.rollback()
        existing = _find_by_key(db, user_id, idempotency_key)
        if existing is not None:
            db.commit()  # end the transaction WITHOUT expiring the row we return
            return _replay(existing, schedule_id, seat_id), False
        raise SeatUnavailable()
