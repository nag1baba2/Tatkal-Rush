"""What a user should SEE for each seat. It must agree with what a booking attempt
would decide, or people click a "free" seat and get a 409.

  AVAILABLE  no active booking, or a HELD one that has expired and has no payment in flight
  HELD       someone is paying (or has time left to), including an expired hold whose payment
             is still PENDING (booking refuses to release those, because money may be moving)
  BOOKED     CONFIRMED
"""

from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import ACTIVE_BOOKING_STATUSES, Booking, Payment, Seat


def seat_states(
    db: Session, schedule_id: int, now: datetime | None = None
) -> list[tuple[Seat, str]]:
    now = now or datetime.now(timezone.utc)
    rows = db.execute(
        select(Seat, Booking.status, Booking.hold_expires_at, Payment.id)
        .outerjoin(
            Booking,
            and_(
                Booking.seat_id == Seat.id,
                Booking.status.in_(ACTIVE_BOOKING_STATUSES),
            ),
        )
        .outerjoin(
            Payment,
            and_(Payment.booking_id == Booking.id, Payment.status == "PENDING"),
        )
        .where(Seat.schedule_id == schedule_id)
        .order_by(Seat.coach, Seat.seat_no)
    ).all()

    out = []
    for seat, status, expires, pending_payment_id in rows:
        if status is None:
            label = "AVAILABLE"
        elif status == "CONFIRMED":
            label = "BOOKED"
        elif expires <= now and pending_payment_id is None:
            label = "AVAILABLE"
        else:
            label = "HELD"
        out.append((seat, label))
    return out
