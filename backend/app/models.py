from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names: the Alembic migration uses the same names,
# and tests can assert on them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


BOOKING_STATUSES = ("HELD", "CONFIRMED", "EXPIRED", "CANCELLED")
# Statuses that occupy a seat (the partial unique index covers exactly these).
ACTIVE_BOOKING_STATUSES = ("HELD", "CONFIRMED")
PAYMENT_STATUSES = ("CREATED", "PENDING", "SUCCESS", "FAILED")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Train(Base):
    __tablename__ = "trains"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(10), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(100))
    destination: Mapped[str] = mapped_column(String(100))


class Schedule(Base):
    """One train on one date. Seats belong to a schedule, not to a train."""

    __tablename__ = "schedules"
    __table_args__ = (
        UniqueConstraint("train_id", "travel_date"),
        CheckConstraint("fare_paise > 0", name="fare_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    train_id: Mapped[int] = mapped_column(ForeignKey("trains.id"))
    travel_date: Mapped[date] = mapped_column(Date)
    departs_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fare_paise: Mapped[int] = mapped_column(Integer)


class Seat(Base):
    __tablename__ = "seats"
    __table_args__ = (
        UniqueConstraint("schedule_id", "coach", "seat_no"),
        # Lets bookings reference (seat, schedule) as a pair, so a booking can
        # never claim a seat that belongs to a different schedule.
        UniqueConstraint("id", "schedule_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedules.id"))
    coach: Mapped[str] = mapped_column(String(10))
    seat_no: Mapped[int] = mapped_column(Integer)


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["seat_id", "schedule_id"], ["seats.id", "seats.schedule_id"]
        ),
        UniqueConstraint("user_id", "idempotency_key"),
        CheckConstraint(
            "status IN ('HELD','CONFIRMED','EXPIRED','CANCELLED')",
            name="status_valid",
        ),
        CheckConstraint(
            "status <> 'HELD' OR hold_expires_at IS NOT NULL",
            name="held_has_expiry",
        ),
        # The last line of defence against double booking: at most one ACTIVE
        # (held or confirmed) booking per seat. Expired/cancelled rows are
        # ignored, so a freed seat can be booked again.
        Index(
            "uq_bookings_active_seat",
            "seat_id",
            unique=True,
            postgresql_where=text("status IN ('HELD','CONFIRMED')"),
        ),
        # Serves the expiry sweep: HELD bookings ordered by expiry.
        Index(
            "ix_bookings_held_expiry",
            "hold_expires_at",
            postgresql_where=text("status = 'HELD'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    seat_id: Mapped[int] = mapped_column(Integer)
    schedule_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    hold_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('CREATED','PENDING','SUCCESS','FAILED')",
            name="status_valid",
        ),
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        # Serves the stale-payment sweep: PENDING payments by last activity.
        Index(
            "ix_payments_pending_updated",
            "updated_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # unique=True -> one payment per booking
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20))
    amount_paise: Mapped[int] = mapped_column(Integer)
    gateway_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    # Why the last attempt failed, or what is unresolved while PENDING.
    last_error: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
