"""Background sweeps. The logic lives here, not in the Celery tasks, so it can be
tested directly and called from anywhere.

Both sweeps are idempotent and safe to run concurrently with each other, with
other workers, and with live bookings and payments.

release_expired_holds
    Marks HELD bookings past their expiry as EXPIRED. This is only housekeeping:
    the booking path already treats an expired hold as free (lazy release), so
    correctness never depends on this running on time.
    * FOR UPDATE SKIP LOCKED: rows another transaction is working on (a payment
      starting, a booking taking the seat, another sweeper) are skipped instead of
      waited for, so concurrent sweepers never double-process or block.
    * A hold with a PENDING payment is never released, because money may be
      moving. The check is repeated AFTER the row lock is held: the candidate
      query ran on an older snapshot, so a payment may have gone PENDING since.

redrive_stale_payments
    Re-runs pay_booking for PENDING payments nobody has touched for a while. That
    is the same code path a user retry takes: it claims the payment and calls the
    gateway with the SAME idempotency key, so it cannot charge twice.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Booking, Payment
from app.services.payment import pay_booking

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def release_expired_holds(
    db: Session, *, now: datetime | None = None, batch_size: int | None = None
) -> int:
    """Expire up to batch_size overdue holds. Returns how many were released."""
    now = now or _utcnow()
    limit = settings.sweep_batch_size if batch_size is None else batch_size

    in_flight = (
        select(Payment.id)
        .where(Payment.booking_id == Booking.id, Payment.status == "PENDING")
        .exists()
    )
    candidates = db.scalars(
        select(Booking)
        .where(
            Booking.status == "HELD",
            Booking.hold_expires_at <= now,
            ~in_flight,
        )
        .order_by(Booking.hold_expires_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()

    released = 0
    for booking in candidates:
        # Re-check under the row lock with a fresh snapshot (see module docstring).
        still_pending = db.scalar(
            select(Payment.id).where(
                Payment.booking_id == booking.id, Payment.status == "PENDING"
            )
        )
        if still_pending is not None:
            continue
        booking.status = "EXPIRED"
        released += 1
    db.commit()
    return released


@dataclass
class RedriveStats:
    success: int = 0
    failed: int = 0
    still_pending: int = 0
    skipped: int = 0  # someone else already had it in hand
    errors: int = 0


def redrive_stale_payments(
    db: Session,
    *,
    gateway=None,
    breaker=None,
    now: datetime | None = None,
    stale_seconds: int | None = None,
    batch_size: int | None = None,
    **pay_kwargs,
) -> RedriveStats:
    now = now or _utcnow()
    stale = settings.payment_stale_seconds if stale_seconds is None else stale_seconds
    limit = settings.sweep_batch_size if batch_size is None else batch_size

    rows = db.execute(
        select(Payment.booking_id, Payment.idempotency_key, Booking.user_id)
        .join(Booking, Booking.id == Payment.booking_id)
        .where(
            Payment.status == "PENDING",
            Payment.updated_at <= now - timedelta(seconds=stale),
        )
        .order_by(Payment.updated_at)
        .limit(limit)
    ).all()
    db.rollback()  # release the connection before the (slow) gateway calls

    stats = RedriveStats()
    for booking_id, key, user_id in rows:
        try:
            result = pay_booking(
                db,
                user_id=user_id,
                booking_id=booking_id,
                idempotency_key=key,
                gateway=gateway,
                breaker=breaker,
                stale_seconds=stale,
                **pay_kwargs,
            )
        except Exception:
            # One bad payment must not stop the rest of the batch. It stays
            # PENDING (and has been claimed), so it is retried on a later sweep.
            db.rollback()
            stats.errors += 1
            log.exception("re-drive failed for booking %s", booking_id)
            continue

        if result.replay:
            stats.skipped += 1
        elif result.payment.status == "SUCCESS":
            stats.success += 1
        elif result.payment.status == "FAILED":
            stats.failed += 1
        else:
            stats.still_pending += 1
    return stats
