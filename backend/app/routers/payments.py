from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.circuit_breaker import CircuitBreaker
from app.db import get_db
from app.models import Booking, Payment, User
from app.schemas import PaymentOut
from app.services.payment import (
    BookingNotFound,
    BookingNotPayable,
    PaymentConflict,
    default_breaker,
    default_gateway,
    pay_booking,
)

router = APIRouter(prefix="/bookings", tags=["payments"])

# 200 paid, 202 still in progress / unresolved, 402 this attempt failed.
STATUS_CODES = {"SUCCESS": 200, "PENDING": 202, "FAILED": 402, "CREATED": 202}


def get_gateway():
    return default_gateway()


def get_breaker() -> CircuitBreaker:
    return default_breaker()


@router.post("/{booking_id}/pay", response_model=PaymentOut)
def pay(
    booking_id: int,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    gateway=Depends(get_gateway),
    breaker: CircuitBreaker = Depends(get_breaker),
):
    """Pay for a held booking.

    200 paid; 402 this attempt failed (the seat stays held, retry with a NEW
    Idempotency-Key); 202 in progress or unresolved (poll or retry later).
    Re-sending the same request never charges twice."""
    try:
        result = pay_booking(
            db,
            user_id=user.id,
            booking_id=booking_id,
            idempotency_key=idempotency_key,
            gateway=gateway,
            breaker=breaker,
        )
    except BookingNotFound:
        raise HTTPException(status_code=404, detail="booking_not_found")
    except BookingNotPayable as e:
        raise HTTPException(
            status_code=409, detail=f"booking_not_payable:{e.booking_status.lower()}"
        )
    except PaymentConflict:
        raise HTTPException(status_code=409, detail="idempotency_key_already_used")

    response.status_code = STATUS_CODES[result.payment.status]
    if result.replay:
        response.headers["Idempotent-Replay"] = "true"
    return result.payment


@router.get("/{booking_id}/payment", response_model=PaymentOut)
def get_payment(
    booking_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    booking = db.get(Booking, booking_id)
    payment = db.scalar(select(Payment).where(Payment.booking_id == booking_id)) if booking else None
    db.commit()  # end the transaction now: a connection must never be held while this request waits for a thread
    if booking is None or booking.user_id != user.id:
        raise HTTPException(status_code=404, detail="booking_not_found")
    if payment is None:
        raise HTTPException(status_code=404, detail="payment_not_found")
    return payment
