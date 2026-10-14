from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import Booking, Payment, Schedule, Seat, Train, User
from app.schemas import BookingCreate, BookingDetailOut, BookingOut
from app.services.booking import (
    IdempotencyKeyReuse,
    SeatNotFound,
    SeatUnavailable,
    create_booking,
)

router = APIRouter(prefix="/bookings", tags=["bookings"])


@router.post("", status_code=201, response_model=BookingOut)
def book_seat(
    body: BookingCreate,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Hold a seat. Send a unique Idempotency-Key per booking attempt; retrying
    with the same key returns the original booking (HTTP 200) instead of
    creating another."""
    try:
        booking, created = create_booking(
            db,
            user_id=user.id,
            schedule_id=body.schedule_id,
            seat_id=body.seat_id,
            idempotency_key=idempotency_key,
        )
    except SeatNotFound:
        raise HTTPException(status_code=404, detail="seat_not_found")
    except SeatUnavailable:
        raise HTTPException(status_code=409, detail="seat_unavailable")
    except IdempotencyKeyReuse:
        raise HTTPException(
            status_code=422, detail="idempotency_key_reused_with_different_request"
        )

    if not created:
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
    return booking


@router.get("", response_model=list[BookingDetailOut])
def my_bookings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The caller's 50 most recent bookings, with the details a screen needs."""
    rows = db.execute(
        select(Booking, Seat, Schedule, Train, Payment.status)
        .join(Seat, Seat.id == Booking.seat_id)
        .join(Schedule, Schedule.id == Booking.schedule_id)
        .join(Train, Train.id == Schedule.train_id)
        .outerjoin(Payment, Payment.booking_id == Booking.id)
        .where(Booking.user_id == user.id)
        .order_by(Booking.id.desc())
        .limit(50)
    ).all()
    db.commit()  # end the transaction now: a connection must never be held while this request waits for a thread
    return [
        BookingDetailOut(
            **BookingOut.model_validate(booking).model_dump(),
            coach=seat.coach,
            seat_no=seat.seat_no,
            travel_date=schedule.travel_date,
            train_number=train.number,
            train_name=train.name,
            fare_paise=schedule.fare_paise,
            payment_status=payment_status,
        )
        for booking, seat, schedule, train, payment_status in rows
    ]


@router.get("/{booking_id}", response_model=BookingOut)
def get_booking(
    booking_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = db.get(Booking, booking_id)
    db.commit()  # end the transaction now: a connection must never be held while this request waits for a thread
    # 404 (not 403) for other users' bookings so ids cannot be probed.
    if booking is None or booking.user_id != user.id:
        raise HTTPException(status_code=404, detail="booking_not_found")
    return booking
