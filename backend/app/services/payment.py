"""Paying for a held seat.

Goals: never charge twice, never charge without giving the seat, never lose track
of money that moved.

Flow of pay_booking():
  1. DECIDE   (short transaction, under the booking row lock)
        Pick one of: replay the stored result, or start/continue an attempt by
        moving the payment to PENDING and COMMITTING that. Concurrent requests
        for the same booking queue on the lock and then see PENDING, so only one
        of them goes on to call the gateway.
  2. CHARGE   (no transaction open, so no DB connection is held)
        Call the gateway with a stable idempotency key (pay-<payment id>-<attempt>),
        through retries with backoff and a circuit breaker. A re-sent call with
        the same key cannot charge twice.
  3. RECORD   (short transaction, booking lock then payment lock)
        SUCCESS  -> payment SUCCESS and booking CONFIRMED, atomically.
        FAILED   -> payment FAILED; the seat stays held so the user can retry.
        UNKNOWN  -> payment stays PENDING. A timeout is ambiguous (the money may
                    have moved), so it is NEVER recorded as FAILED. Once the
                    payment is stale it is re-driven with the SAME gateway key,
                    which returns the original result instead of charging again.

A hold with a PENDING payment cannot be expired or released (see booking.py), so a
successful charge always finds its seat still held.

Lock order everywhere: seat -> booking -> payment. No path takes them in reverse.
"""

import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

import redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.config import settings
from app.gateway import (
    FakeGateway,
    GatewayDeclined,
    GatewayTimeout,
    GatewayUnavailable,
)
from app.models import Booking, Payment, Schedule
from app.retry import retry_with_backoff
from app.services.payment_state import transition


class PaymentError(Exception):
    pass


class BookingNotFound(PaymentError):
    pass


class BookingNotPayable(PaymentError):
    def __init__(self, booking_status: str):
        super().__init__(booking_status)
        self.booking_status = booking_status


class PaymentConflict(PaymentError):
    """The idempotency key is already used by another payment."""


class InvariantViolation(PaymentError):
    """A charge succeeded but the booking was not HELD. Must never happen."""


@dataclass
class PayResult:
    payment: Payment
    replay: bool  # True if no new gateway work was done for this request


@dataclass
class ChargeOutcome:
    kind: str  # "success" | "failed" | "unknown"
    ref: str | None = None
    reason: str | None = None


@lru_cache
def default_gateway() -> FakeGateway:
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    return FakeGateway(
        client,
        fail_rate=settings.gateway_fail_rate,
        lost_response_rate=settings.gateway_lost_response_rate,
        decline_rate=settings.gateway_decline_rate,
        latency_ms=settings.gateway_latency_ms,
    )


@lru_cache
def default_breaker() -> CircuitBreaker:
    return CircuitBreaker(
        failure_threshold=settings.breaker_failure_threshold,
        recovery_timeout=settings.breaker_recovery_seconds,
    )


def gateway_key(payment: Payment) -> str:
    """Stable per attempt: re-driving the same attempt reuses it, a new attempt
    after a definite failure gets a new one."""
    return f"pay-{payment.id}-{payment.attempts}"


def charge_with_resilience(
    gateway,
    breaker: CircuitBreaker,
    key: str,
    amount_paise: int,
    *,
    attempts: int,
    base_delay: float,
    max_delay: float,
    sleep,
    rng,
) -> ChargeOutcome:
    saw_timeout = False

    def one_attempt():
        nonlocal saw_timeout
        try:
            # A decline is a valid answer from a healthy gateway, so it must not
            # count towards opening the circuit.
            return breaker.call(
                gateway.charge,
                key,
                amount_paise,
                is_failure=lambda exc: not isinstance(exc, GatewayDeclined),
            )
        except GatewayTimeout:
            saw_timeout = True
            raise

    try:
        result = retry_with_backoff(
            one_attempt,
            attempts=attempts,
            base_delay=base_delay,
            max_delay=max_delay,
            retry_on=(GatewayUnavailable, GatewayTimeout),
            sleep=sleep,
            rng=rng,
        )
        return ChargeOutcome("success", ref=result.ref)
    except GatewayDeclined:
        return ChargeOutcome("failed", reason="declined")
    except CircuitOpenError:
        if saw_timeout:
            return ChargeOutcome("unknown", reason="gateway_timeout")
        return ChargeOutcome("failed", reason="gateway_unavailable")
    except (GatewayUnavailable, GatewayTimeout):
        if saw_timeout:
            return ChargeOutcome("unknown", reason="gateway_timeout")
        return ChargeOutcome("failed", reason="retries_exhausted")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _begin_attempt(
    db: Session, user_id: int, booking_id: int, idempotency_key: str, stale_seconds: int
) -> tuple[Payment, bool]:
    """Step 1. Returns (payment, replay). replay=True means: nothing to charge."""
    booking = db.scalar(
        select(Booking)
        .where(Booking.id == booking_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if booking is None or booking.user_id != user_id:
        db.rollback()
        raise BookingNotFound()

    payment = db.scalar(
        select(Payment)
        .where(Payment.booking_id == booking.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = _utcnow()

    if payment is not None:
        if payment.status == "SUCCESS":
            db.commit()  # ends the transaction without expiring the payment we return
            return payment, True
        if payment.status == "PENDING":
            age = (now - payment.updated_at).total_seconds()
            if age < stale_seconds:
                db.commit()  # someone is on it right now
                return payment, True
            # Stale: claim the re-drive by refreshing updated_at, so concurrent
            # requests see it as in progress. The gateway key is unchanged.
            payment.updated_at = now
            db.commit()
            return payment, False
        # FAILED
        if payment.idempotency_key == idempotency_key:
            db.commit()  # same request again: same answer
            return payment, True

    if booking.status != "HELD" or booking.hold_expires_at <= now:
        status = booking.status
        db.rollback()
        raise BookingNotPayable(status)

    try:
        if payment is None:
            fare = db.get(Schedule, booking.schedule_id).fare_paise
            payment = Payment(
                booking_id=booking.id,
                status="CREATED",
                amount_paise=fare,
                idempotency_key=idempotency_key,
                attempts=0,
            )
            db.add(payment)
            db.flush()
        else:  # FAILED -> new attempt with a new request key
            payment.idempotency_key = idempotency_key
        transition(payment, "PENDING")
        payment.attempts += 1
        payment.last_error = None
        payment.updated_at = now
        db.commit()
    except IntegrityError:
        db.rollback()
        raise PaymentConflict()
    return payment, False


def _finalize(
    db: Session, payment_id: int, booking_id: int, outcome: ChargeOutcome
) -> Payment:
    """Step 3. Lock order: booking, then payment."""
    # populate_existing: this session loaded these rows in step 1; after taking
    # the locks we must see their CURRENT state, not the copy from step 1.
    booking = db.scalar(
        select(Booking)
        .where(Booking.id == booking_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    payment = db.scalar(
        select(Payment)
        .where(Payment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if payment.status != "PENDING":
        db.commit()  # another request already resolved it
        return payment

    if outcome.kind == "success":
        if booking.status != "HELD":
            db.rollback()
            raise InvariantViolation(
                f"charged payment {payment_id} but booking is {booking.status}"
            )
        transition(payment, "SUCCESS")
        payment.gateway_ref = outcome.ref
        payment.last_error = None
        booking.status = "CONFIRMED"
    elif outcome.kind == "failed":
        transition(payment, "FAILED")
        payment.last_error = outcome.reason
    else:  # unknown: stay PENDING, re-driven later with the same key
        payment.last_error = outcome.reason
    # Set updated_at here rather than leaving it to the database's onupdate: a value the
    # database fills in leaves the attribute unloaded, and reading it while the HTTP
    # response is built would silently run a query (and need a connection) in a worker thread.
    payment.updated_at = _utcnow()
    db.commit()
    return payment


def pay_booking(
    db: Session,
    *,
    user_id: int,
    booking_id: int,
    idempotency_key: str,
    gateway=None,
    breaker: CircuitBreaker | None = None,
    sleep=time.sleep,
    rng=random.random,
    attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    stale_seconds: int | None = None,
) -> PayResult:
    gateway = gateway or default_gateway()
    breaker = breaker or default_breaker()

    payment, replay = _begin_attempt(
        db,
        user_id,
        booking_id,
        idempotency_key,
        settings.payment_stale_seconds if stale_seconds is None else stale_seconds,
    )
    if replay:
        return PayResult(payment, True)

    # No transaction is open here, so no DB connection is held during the call.
    outcome = charge_with_resilience(
        gateway,
        breaker,
        gateway_key(payment),
        payment.amount_paise,
        attempts=settings.retry_max_attempts if attempts is None else attempts,
        base_delay=settings.retry_base_delay if base_delay is None else base_delay,
        max_delay=settings.retry_max_delay if max_delay is None else max_delay,
        sleep=sleep,
        rng=rng,
    )
    payment = _finalize(db, payment.id, booking_id, outcome)
    return PayResult(payment, False)
