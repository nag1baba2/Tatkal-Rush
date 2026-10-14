"""release_expired_holds and redrive_stale_payments: single-threaded behaviour."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.gateway import GatewayDeclined, GatewayTimeout
from app.models import Booking, Payment, Seat, User
from app.services.booking import create_booking
from app.services.expiry import redrive_stale_payments, release_expired_holds
from tests.helpers import (
    ScriptedGateway,
    expire_hold,
    hold,
    make_stale,
    new_breaker,
    no_sleep,
    pay,
)

PAST = timedelta(minutes=-1)
FUTURE = timedelta(minutes=5)


def add_bookings(db, world, n, *, offset=PAST, status="HELD", start=0):
    """n extra bookings (own user and seat each) in schedule s1."""
    out = []
    for i in range(start, start + n):
        user = User(email=f"extra{i}@example.com", password_hash="x")
        db.add(user)
        db.flush()
        seat = Seat(schedule_id=world["s1"].id, coach="S9", seat_no=i + 1)
        db.add(seat)
        db.flush()
        booking = Booking(
            user_id=user.id,
            seat_id=seat.id,
            schedule_id=world["s1"].id,
            status=status,
            hold_expires_at=datetime.now(timezone.utc) + offset if status == "HELD" else None,
            idempotency_key=f"extra-{i}",
        )
        db.add(booking)
        out.append(booking)
    db.flush()
    db.commit()
    return out


def statuses(db, bookings):
    db.expire_all()
    return [db.get(Booking, b.id).status for b in bookings]


def add_payment(db, booking, status):
    """A payment in the given state, created through the legal transitions."""
    p = Payment(booking_id=booking.id, status="CREATED", amount_paise=100,
                idempotency_key=f"pk-{booking.id}")
    db.add(p)
    db.flush()
    if status != "CREATED":
        db.execute(text("UPDATE payments SET status='PENDING' WHERE id=:i"), {"i": p.id})
        if status in ("SUCCESS", "FAILED"):
            db.execute(text("UPDATE payments SET status=:s WHERE id=:i"), {"s": status, "i": p.id})
    db.commit()
    return p


# ---- release_expired_holds -------------------------------------------------------

def test_releases_only_expired_held_bookings(db, world):
    expired = add_bookings(db, world, 2, offset=PAST)
    live = add_bookings(db, world, 2, offset=FUTURE, start=10)
    confirmed = add_bookings(db, world, 1, status="CONFIRMED", start=20)

    assert release_expired_holds(db) == 2

    assert statuses(db, expired) == ["EXPIRED", "EXPIRED"]
    assert statuses(db, live) == ["HELD", "HELD"]
    assert statuses(db, confirmed) == ["CONFIRMED"]


def test_running_it_again_releases_nothing(db, world):
    add_bookings(db, world, 3)
    assert release_expired_holds(db) == 3
    assert release_expired_holds(db) == 0


def test_a_hold_with_a_pending_payment_is_not_released(db, world):
    (booking,) = add_bookings(db, world, 1)
    payment = add_payment(db, booking, "PENDING")

    assert release_expired_holds(db) == 0
    assert statuses(db, [booking]) == ["HELD"]

    db.execute(text("UPDATE payments SET status='FAILED' WHERE id=:i"), {"i": payment.id})
    db.commit()
    assert release_expired_holds(db) == 1  # nothing in flight any more
    assert statuses(db, [booking]) == ["EXPIRED"]


def test_a_hold_with_a_failed_payment_is_released(db, world):
    (booking,) = add_bookings(db, world, 1)
    add_payment(db, booking, "FAILED")
    assert release_expired_holds(db) == 1


def test_batch_size_limits_one_run(db, world):
    add_bookings(db, world, 5)
    assert [release_expired_holds(db, batch_size=2) for _ in range(4)] == [2, 2, 1, 0]


def test_the_now_argument_decides_what_is_overdue(db, world):
    live = add_bookings(db, world, 2, offset=FUTURE)
    assert release_expired_holds(db) == 0
    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert release_expired_holds(db, now=later) == 2
    assert statuses(db, live) == ["EXPIRED", "EXPIRED"]


def test_a_released_seat_can_be_booked_by_someone_else(db, world):
    old = hold(db, world, user="u1", seconds=60)
    expire_hold(db, old)
    assert release_expired_holds(db) == 1
    assert statuses(db, [old]) == ["EXPIRED"]  # released by the sweep, not by the booking
    new, created = create_booking(
        db, user_id=world["u2"].id, schedule_id=world["s1"].id,
        seat_id=world["seat1"].id, idempotency_key="b2",
    )
    assert created and new.status == "HELD"


# ---- redrive_stale_payments ----------------------------------------------------------

def pending_payment(db, world, *, key="p1"):
    booking = hold(db, world)
    gw = ScriptedGateway(default=GatewayTimeout())
    payment = pay(db, world, booking, gw, key=key).payment
    assert payment.status == "PENDING"
    return booking, payment, gw.calls[0][0]


def redrive(db, gateway, **kw):
    kw.setdefault("breaker", new_breaker())
    return redrive_stale_payments(db, gateway=gateway, sleep=no_sleep, rng=lambda: 0.0, **kw)


def test_a_stale_pending_payment_is_resolved_with_its_original_key(db, world):
    booking, payment, original_key = pending_payment(db, world)
    make_stale(db)

    gw = ScriptedGateway("gw_found")
    stats = redrive(db, gw)

    assert (stats.success, stats.failed, stats.still_pending, stats.errors) == (1, 0, 0, 0)
    assert gw.calls[0][0] == original_key  # same key => the gateway dedupes
    db.expire_all()
    assert db.get(Payment, payment.id).status == "SUCCESS"
    assert db.get(Booking, booking.id).status == "CONFIRMED"


def test_a_fresh_pending_payment_is_left_alone(db, world):
    pending_payment(db, world)
    stats = redrive(db, ScriptedGateway())  # a gateway call would raise
    assert vars(stats) == {"success": 0, "failed": 0, "still_pending": 0, "skipped": 0, "errors": 0}


def test_finished_payments_are_left_alone(db, world):
    b1 = hold(db, world, key="b1")
    pay(db, world, b1, ScriptedGateway("gw_1"), key="p1")  # SUCCESS
    b2 = hold(db, world, seat="seat2", sched="s2", key="b2")
    pay(db, world, b2, ScriptedGateway(GatewayDeclined()), key="p2")  # FAILED
    make_stale(db)
    stats = redrive(db, ScriptedGateway())
    assert sum(vars(stats).values()) == 0


def test_a_payment_that_stays_unresolved_is_not_hammered(db, world):
    pending_payment(db, world)
    make_stale(db)

    first = redrive(db, ScriptedGateway(default=GatewayTimeout()))
    assert first.still_pending == 1

    again = redrive(db, ScriptedGateway())  # the claim made it fresh: no gateway call
    assert sum(vars(again).values()) == 0


def test_a_declined_redrive_resolves_to_failed(db, world):
    booking, payment, _ = pending_payment(db, world)
    make_stale(db)
    stats = redrive(db, ScriptedGateway(GatewayDeclined()))
    assert stats.failed == 1
    db.expire_all()
    assert db.get(Payment, payment.id).status == "FAILED"
    assert db.get(Booking, booking.id).status == "HELD"  # seat still held for a retry


def test_one_broken_payment_does_not_stop_the_batch(db, world):
    pending_payment(db, world, key="p1")
    b2 = hold(db, world, seat="seat2", sched="s2", key="b2")
    pay(db, world, b2, ScriptedGateway(default=GatewayTimeout()), key="p2")
    make_stale(db)

    gw = ScriptedGateway(RuntimeError("boom"), "gw_ok")
    stats = redrive(db, gw)

    assert stats.errors == 1 and stats.success == 1
    assert len(gw.calls) == 2


def test_the_redrive_batch_size_is_respected(db, world):
    pending_payment(db, world, key="p1")
    b2 = hold(db, world, seat="seat2", sched="s2", key="b2")
    pay(db, world, b2, ScriptedGateway(default=GatewayTimeout()), key="p2")
    make_stale(db)
    stats = redrive(db, ScriptedGateway("gw_1", "gw_2"), batch_size=1)
    assert stats.success == 1
