"""Payments under real concurrency, against a gateway that fails randomly.

The gateway's Redis ledger is the "bank statement": every real charge is one
entry. The central claims, checked against it:

  * a payment is never charged twice;
  * a FAILED payment was never charged;
  * a SUCCESS payment was charged exactly once and its booking is CONFIRMED;
  * after unresolved (PENDING) payments are re-driven, no money is unaccounted for.
"""

import random
import re
from collections import Counter

from sqlalchemy import select, text

from app.circuit_breaker import CircuitBreaker
from app.db import SessionLocal
from app.models import Booking, Payment
from app.services.booking import create_booking
from app.services.payment import pay_booking
from tests.helpers import committed_world, no_sleep, run_parallel

KEY_RE = re.compile(r"^pay-(\d+)-(\d+)$")


def never_trips():
    return CircuitBreaker(failure_threshold=10**6, recovery_timeout=1)


def hold_all(world):
    """One HELD booking per user/seat pair; returns the booking ids."""
    ids = []
    with SessionLocal() as s:
        for i, (uid, sid) in enumerate(zip(world.user_ids, world.seat_ids)):
            booking, _ = create_booking(
                s,
                user_id=uid,
                schedule_id=world.schedule_id,
                seat_id=sid,
                idempotency_key=f"book-{i}",
            )
            ids.append(booking.id)
    return ids


def charges_per_payment(gateway) -> Counter:
    counts = Counter()
    for entry in gateway.ledger_entries():
        m = KEY_RE.match(entry["key"])
        assert m, f"unexpected gateway key {entry['key']!r}"
        counts[int(m.group(1))] += 1
    return counts


def read_state(booking_ids):
    with SessionLocal() as s:
        payments = s.scalars(select(Payment).where(Payment.booking_id.in_(booking_ids))).all()
        bookings = {
            b.id: b.status
            for b in s.scalars(select(Booking).where(Booking.id.in_(booking_ids))).all()
        }
    return payments, bookings


def test_50_parallel_payments_for_one_booking_charge_exactly_once(pool_sessions, make_gateway):
    gateway = make_gateway(fail_rate=0, lost_response_rate=0, decline_rate=0)
    breaker = never_trips()
    with committed_world(n_users=1, n_seats=1) as w:
        (booking_id,) = hold_all(w)

        def attempt(_):
            with pool_sessions() as s:
                result = pay_booking(
                    s, user_id=w.user_ids[0], booking_id=booking_id,
                    idempotency_key="pay-once", gateway=gateway, breaker=breaker,
                    sleep=no_sleep,
                )
                return result.payment.status

        results = run_parallel(attempt, list(range(50)))

        assert len(gateway.ledger_entries()) == 1  # ONE charge for 50 requests
        assert set(results) <= {"SUCCESS", "PENDING"}  # PENDING = "someone is on it"
        assert "SUCCESS" in results
        payments, bookings = read_state([booking_id])
        assert len(payments) == 1
        assert payments[0].status == "SUCCESS"
        assert bookings[booking_id] == "CONFIRMED"


def test_50_parallel_payments_with_different_keys_still_charge_once(pool_sessions, make_gateway):
    """Retries from a buggy or impatient client that invents a new key each time."""
    gateway = make_gateway(fail_rate=0, lost_response_rate=0, decline_rate=0)
    breaker = never_trips()
    with committed_world(n_users=1, n_seats=1) as w:
        (booking_id,) = hold_all(w)

        def attempt(i):
            with pool_sessions() as s:
                pay_booking(
                    s, user_id=w.user_ids[0], booking_id=booking_id,
                    idempotency_key=f"fresh-key-{i}", gateway=gateway, breaker=breaker,
                    sleep=no_sleep,
                )

        run_parallel(attempt, list(range(50)))

        assert len(gateway.ledger_entries()) == 1
        payments, bookings = read_state([booking_id])
        assert len(payments) == 1 and payments[0].status == "SUCCESS"
        assert bookings[booking_id] == "CONFIRMED"


def test_chaos_never_double_charges_and_leaves_no_orphans(pool_sessions, make_gateway):
    n = 60
    chaos = make_gateway(
        fail_rate=0.2, lost_response_rate=0.6, decline_rate=0.05,
        lost_replay_rate=0.6, rng=random.Random(42),
    )
    breaker = never_trips()
    with committed_world(n_users=n, n_seats=n) as w:
        booking_ids = hold_all(w)

        # Every booking is paid by 3 simultaneous duplicate requests.
        jobs = [(i, dup) for i in range(n) for dup in range(3)]

        def attempt(job):
            i, _ = job
            with pool_sessions() as s:
                pay_booking(
                    s, user_id=w.user_ids[i], booking_id=booking_ids[i],
                    idempotency_key=f"pay-req-{i}", gateway=chaos, breaker=breaker,
                    sleep=no_sleep, rng=lambda: 0.0,
                )

        run_parallel(attempt, jobs)

        injected = chaos.stats()
        assert injected.get("timeouts", 0) + injected.get("unavailable", 0) + injected.get("declines", 0) > 0

        # ---- after the chaos ------------------------------------------------
        payments, bookings = read_state(booking_ids)
        charged = charges_per_payment(chaos)
        # The run must have produced unresolved payments, otherwise the
        # re-drive part below would prove nothing.
        assert any(p.status == "PENDING" for p in payments), "chaos produced no PENDING payments"
        assert len(payments) == len({p.booking_id for p in payments})  # <= 1 per booking
        assert all(count <= 1 for count in charged.values()), "a payment was charged twice"
        for p in payments:
            if p.status == "SUCCESS":
                assert charged[p.id] == 1
                assert bookings[p.booking_id] == "CONFIRMED"
            elif p.status == "FAILED":
                assert charged[p.id] == 0, "a FAILED payment was actually charged"
                assert bookings[p.booking_id] == "HELD"
            else:  # PENDING: unresolved, money may or may not have moved
                assert p.status == "PENDING"
                assert charged[p.id] in (0, 1)
                assert bookings[p.booking_id] == "HELD"

        # ---- re-drive every unresolved payment with a healthy gateway ---------
        pending = [p for p in payments if p.status == "PENDING"]
        with SessionLocal() as s:
            s.execute(
                text("UPDATE payments SET updated_at = now() - interval '1 hour' "
                     "WHERE status = 'PENDING' AND booking_id = ANY(:ids)"),
                {"ids": booking_ids},
            )
            s.commit()
        healthy = make_gateway(prefix=chaos.prefix, fail_rate=0, lost_response_rate=0, decline_rate=0)
        user_of = dict(zip(booking_ids, w.user_ids))
        for p in pending:
            with SessionLocal() as s:
                pay_booking(
                    s, user_id=user_of[p.booking_id], booking_id=p.booking_id,
                    idempotency_key=p.idempotency_key, gateway=healthy,
                    breaker=never_trips(), sleep=no_sleep,
                )

        payments, bookings = read_state(booking_ids)
        charged = charges_per_payment(healthy)
        statuses = Counter(p.status for p in payments)
        assert statuses["PENDING"] == 0, "unresolved payments remain"
        assert all(count <= 1 for count in charged.values()), "a payment was charged twice"
        for p in payments:
            if p.status == "SUCCESS":
                assert charged[p.id] == 1
                assert bookings[p.booking_id] == "CONFIRMED"
            else:
                assert p.status == "FAILED"
                assert charged[p.id] == 0
                assert bookings[p.booking_id] == "HELD"
        # No stray money: every ledger entry belongs to a SUCCESS payment.
        assert len(healthy.ledger_entries()) == statuses["SUCCESS"]
        assert set(charged) == {p.id for p in payments if p.status == "SUCCESS"}
        # The run should have produced a real mix, not trivially all one outcome.
        assert statuses["SUCCESS"] > 0 and statuses["FAILED"] > 0
