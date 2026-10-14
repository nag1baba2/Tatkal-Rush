"""pay_booking(): every path, with scripted or controlled gateways."""

from uuid import uuid4

import pytest
import redis
from sqlalchemy import func, select, text

from app.circuit_breaker import CircuitBreaker
from app.config import settings
from app.gateway import (
    FakeGateway,
    GatewayDeclined,
    GatewayTimeout,
    GatewayUnavailable,
)
from app.models import Booking, Payment
from app.services.booking import SeatUnavailable, create_booking
from app.services.payment import (
    BookingNotFound,
    BookingNotPayable,
    PaymentConflict,
    pay_booking,
)
from tests.helpers import (
    ScriptedGateway,
    expire_hold,
    hold,
    make_stale,
    new_breaker,
    no_sleep,
    pay,
)

FARE = 50000  # set by the `world` fixture


def payment_count(db):
    return db.scalar(select(func.count()).select_from(Payment))


# ---- the happy path -----------------------------------------------------------

def test_successful_payment_confirms_the_booking(db, world):
    booking = hold(db, world)
    gw = ScriptedGateway("gw_ok")
    result = pay(db, world, booking, gw)

    p = result.payment
    assert result.replay is False
    assert (p.status, p.attempts, p.gateway_ref, p.amount_paise) == ("SUCCESS", 1, "gw_ok", FARE)
    db.refresh(booking)
    assert booking.status == "CONFIRMED"
    assert gw.calls == [(f"pay-{p.id}-1", FARE)]


def test_transient_failures_are_retried_with_the_same_key(db, world):
    booking = hold(db, world)
    gw = ScriptedGateway(GatewayUnavailable(), GatewayUnavailable(), "gw_ok")
    p = pay(db, world, booking, gw).payment

    assert p.status == "SUCCESS"
    assert len(gw.calls) == 3
    assert len({key for key, _ in gw.calls}) == 1  # one idempotency key throughout


# ---- failures --------------------------------------------------------------------

def test_exhausted_retries_fail_the_attempt_but_keep_the_seat_held(db, world):
    booking = hold(db, world)
    gw = ScriptedGateway(default=GatewayUnavailable())
    p = pay(db, world, booking, gw).payment

    assert (p.status, p.last_error) == ("FAILED", "retries_exhausted")
    assert len(gw.calls) == 4
    db.refresh(booking)
    assert booking.status == "HELD"


def test_user_can_retry_a_failed_payment_with_a_new_key(db, world):
    booking = hold(db, world)
    first = pay(db, world, booking, ScriptedGateway(default=GatewayUnavailable()), key="p1")
    assert first.payment.status == "FAILED"

    gw = ScriptedGateway("gw_ok")
    second = pay(db, world, booking, gw, key="p2")
    p = second.payment

    assert (p.status, p.attempts, p.idempotency_key) == ("SUCCESS", 2, "p2")
    assert gw.calls[0][0] == f"pay-{p.id}-2"  # a new attempt gets a new gateway key
    assert payment_count(db) == 1  # still one payment row for the booking
    db.refresh(booking)
    assert booking.status == "CONFIRMED"


def test_a_decline_fails_immediately_without_retrying(db, world):
    booking = hold(db, world)
    gw = ScriptedGateway(GatewayDeclined())
    p = pay(db, world, booking, gw).payment
    assert (p.status, p.last_error) == ("FAILED", "declined")
    assert len(gw.calls) == 1


def test_same_key_after_a_failure_replays_the_failure(db, world):
    booking = hold(db, world)
    pay(db, world, booking, ScriptedGateway(GatewayDeclined()), key="p1")
    again = pay(db, world, booking, ScriptedGateway(), key="p1")  # must not call
    assert again.replay is True
    assert again.payment.status == "FAILED"


def test_open_circuit_fails_fast_without_calling_the_gateway(db, world):
    booking = hold(db, world)
    gw = ScriptedGateway(default=GatewayUnavailable())
    p = pay(db, world, booking, gw, breaker=new_breaker(threshold=1)).payment

    assert (p.status, p.last_error) == ("FAILED", "gateway_unavailable")
    assert len(gw.calls) == 1  # one real call opened the circuit; the rest were refused


def test_card_declines_do_not_trip_the_circuit(db, world):
    """A decline is a normal answer from a healthy gateway, not an outage."""
    booking = hold(db, world)
    breaker = new_breaker(threshold=1)  # one counted failure would open it
    pay(db, world, booking, ScriptedGateway(GatewayDeclined()), key="p1", breaker=breaker)
    assert breaker.state == "closed"

    gw = ScriptedGateway("gw_ok")  # the gateway is still reachable on the next try
    assert pay(db, world, booking, gw, key="p2", breaker=breaker).payment.status == "SUCCESS"


# ---- idempotency ---------------------------------------------------------------------

def test_paying_again_never_charges_again(db, world):
    booking = hold(db, world)
    pay(db, world, booking, ScriptedGateway("gw_ok"), key="p1")

    same_key = pay(db, world, booking, ScriptedGateway(), key="p1")
    other_key = pay(db, world, booking, ScriptedGateway(), key="p2")

    assert same_key.replay and other_key.replay
    assert same_key.payment.status == other_key.payment.status == "SUCCESS"
    assert payment_count(db) == 1


def test_a_key_used_by_another_booking_is_a_conflict(db, world):
    b1 = hold(db, world, key="b1")
    b2 = hold(db, world, seat="seat2", sched="s2", key="b2")
    pay(db, world, b1, ScriptedGateway("gw_1"), key="same")
    with pytest.raises(PaymentConflict):
        pay(db, world, b2, ScriptedGateway(), key="same")
    assert payment_count(db) == 1


# ---- ambiguity: the dangerous case ---------------------------------------------------------

def test_a_lost_response_is_recovered_by_retrying_with_the_same_key(db, world):
    """The gateway charges, but the response is lost. The retry must get the
    original charge back, not create a second one."""
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    gw = FakeGateway(client, fail_rate=0, lost_response_rate=1.0, decline_rate=0,
                     lost_replay_rate=0.0, prefix=f"gwtest:{uuid4().hex}:")
    try:
        booking = hold(db, world)
        p = pay(db, world, booking, gw).payment

        assert p.status == "SUCCESS"
        assert len(gw.ledger_entries()) == 1
        assert gw.stats()["timeouts"] == 1
    finally:
        gw.reset()


def test_all_timeouts_leave_the_payment_pending_not_failed(db, world):
    booking = hold(db, world)
    p = pay(db, world, booking, ScriptedGateway(default=GatewayTimeout())).payment

    assert p.status == "PENDING"  # we cannot know whether money moved
    assert p.last_error == "gateway_timeout"
    db.refresh(booking)
    assert booking.status == "HELD"


def test_a_stale_pending_payment_is_resolved_with_the_same_gateway_key(db, world):
    booking = hold(db, world)
    first_gw = ScriptedGateway(default=GatewayTimeout())
    p = pay(db, world, booking, first_gw).payment
    original_key = first_gw.calls[0][0]

    # Asking again straight away must not start a second charge.
    busy = pay(db, world, booking, ScriptedGateway())
    assert busy.replay is True and busy.payment.status == "PENDING"

    make_stale(db)
    second_gw = ScriptedGateway("gw_found")
    result = pay(db, world, booking, second_gw)

    assert result.replay is False
    assert result.payment.status == "SUCCESS"
    assert second_gw.calls[0][0] == original_key  # same key => gateway dedupes
    db.refresh(booking)
    assert booking.status == "CONFIRMED"


# ---- what may be paid ---------------------------------------------------------------------------

def test_someone_elses_booking_is_not_found(db, world):
    booking = hold(db, world, user="u1")
    with pytest.raises(BookingNotFound):
        pay(db, world, booking, ScriptedGateway(), user="u2")
    with pytest.raises(BookingNotFound):
        pay_booking(db, user_id=world["u1"].id, booking_id=0, idempotency_key="x",
                    gateway=ScriptedGateway())
    assert payment_count(db) == 0


def test_an_expired_hold_cannot_be_paid(db, world):
    booking = hold(db, world, seconds=0)
    with pytest.raises(BookingNotPayable):
        pay(db, world, booking, ScriptedGateway())
    assert payment_count(db) == 0


# ---- a payment in flight protects the hold ------------------------------------------------------

def test_a_hold_with_a_pending_payment_is_not_released(db, world):
    booking = hold(db, world, user="u1")
    pay(db, world, booking, ScriptedGateway(default=GatewayTimeout()))  # PENDING
    expire_hold(db, booking)

    with pytest.raises(SeatUnavailable):  # u2 cannot take the seat: money may be moving
        hold(db, world, user="u2", key="b2")

    make_stale(db)
    result = pay(db, world, booking, ScriptedGateway("gw_found"))
    assert result.payment.status == "SUCCESS"
    db.refresh(booking)
    assert booking.status == "CONFIRMED"  # the payer still got the seat


def test_a_failed_payment_does_not_protect_an_expired_hold(db, world):
    booking = hold(db, world, user="u1")
    pay(db, world, booking, ScriptedGateway(GatewayDeclined()))  # FAILED
    expire_hold(db, booking)

    new = hold(db, world, user="u2", key="b2")  # allowed: nothing is in flight
    assert new.status == "HELD"
    db.refresh(booking)
    assert booking.status == "EXPIRED"
