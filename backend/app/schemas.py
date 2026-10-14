import re
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, field_validator

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Credentials(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if len(v) > 255 or not EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v

    @field_validator("password")
    @classmethod
    def valid_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("password must be at least 8 characters")
        if len(v.encode()) > 72:  # bcrypt only uses the first 72 bytes
            raise ValueError("password must be at most 72 bytes")
        return v


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class BookingCreate(BaseModel):
    schedule_id: int
    seat_id: int


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    schedule_id: int
    seat_id: int
    status: str
    hold_expires_at: datetime | None
    idempotency_key: str
    created_at: datetime


class TrainOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: str
    name: str
    source: str
    destination: str


class ScheduleOut(BaseModel):
    id: int
    train_id: int
    travel_date: date
    departs_at: datetime
    fare_paise: int
    seats_total: int
    seats_available: int


class BookingDetailOut(BookingOut):
    """A booking with the details a screen needs to show it."""

    coach: str
    seat_no: int
    travel_date: date
    train_number: str
    train_name: str
    fare_paise: int
    payment_status: str | None


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    booking_id: int
    status: str  # CREATED | PENDING | SUCCESS | FAILED
    amount_paise: int
    gateway_ref: str | None
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class SeatOut(BaseModel):
    id: int
    coach: str
    seat_no: int
    status: str  # AVAILABLE | HELD | BOOKED
