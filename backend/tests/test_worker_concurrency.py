"""The sweeps racing against live bookings, live payments, and each other.

The suite-wide fixture pauses the live worker, so the only sweeping that happens
here is the sweeping these tests start themselves.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.circuit_breaker import CircuitBreaker
from app.db import SessionLocal
from app.gateway import ChargeResult, GatewayTimeout
from app.models import Booking, Payment
from app.services.booking import SeatUnavailable, create_booking
from app.services.expiry import redrive_stale_payments, release_expired_holds
from app.services.payment import pay_booking
from tests.helpers import ScriptedGateway, committed_world, no_sleep, run_parallel

PAST = timedelta(minutes=-1)


def insert_held(world, user_id, seat_id, key, offset=PAST) -> int:
    with SessionLocal() as s:
        booking = Booking(
            user_id=user_id, seat_id=seat_id, schedule_id=world.schedule_id,
            status="HELD", hold_expires_at=datetime.now(timezone.utc) + offset,
            idempotency_key=key,
        )
        s.add(booking)
        s.commit()
        return booking.id


def status_of(booking_id):
    with SessionLocal() as s:
        return s.get(Booking, booking_id).status


def active_bookings(seat_id):
    with SessionLocal() as s:
        return s.scalars(
            select(Booking).where(
                Booking.seat_id == seat_id, Booking.status.in_(("HELD", "CONFIRMED"))
            )
        ).all()


def test_sweepers_and_new_bookers_race_over_expired_holds(pool_sessions):
    """40 seats, each with an expired hold, 3 new bookers per seat and 4 sweepers all
    at once. Whoever frees a seat (the sweep or the booking's own lazy release), each
    seat must end up with exactly one new booker and an EXPIRED old hold."""
    n = 40
    with committed_world(n_users=n * 4, n_seats=n) as w:
        old_users, contenders = w.user_ids[:n], w.user_ids[n:]
        old_ids = [insert_held(w, old_users[i], w.seat_ids[i], f"old-{i}") for i in range(n)]

        def booker(j):
            with pool_sessions() as s:
                try:
                    create_booking(s, user_id=contenders[j], schedule_id=w.schedule_id,
                                   seat_id=w.seat_ids[j % n], idempotency_key=f"new-{j}")
                    return (j % n, "booked")
                except SeatUnavailable:
                    return (j % n, "taken")

        def sweeper(_):
            released = 0
            for _ in range(5):
                with pool_sessions() as s:
                    released += release_expired_holds(s)
            return ("sweeper", released)

        jobs = [lambda j=j: booker(j) for j in range(n * 3)] + [lambda: sweeper(0)] * 4
        results = run_parallel(lambda job: job(), jobs)

        booked_per_seat = {}
        for result in results:
            if result[1] == "booked":
                booked_per_seat[result[0]] = booked_per_seat.get(result[0], 0) + 1
        assert booked_per_seat == {i: 1 for i in range(n)}, "each seat needs exactly one new booker"

        for i, seat_id in enumerate(w.seat_ids):
            active = active_bookings(seat_id)
            assert len(active) == 1
            assert active[0].user_id in contenders
        assert [status_of(b) for b in old_ids] == ["EXPIRED"] * n


def test_parallel_sweepers_release_each_hold_exactly_once(pool_sessions):
    n = 100
    with committed_world(n_users=n, n_seats=n) as w:
        ids = [insert_held(w, w.user_ids[i], w.seat_ids[i], f"k{i}") for i in range(n)]

        def sweeper(_):
            total = 0
            while True:
                with pool_sessions() as s:
                    released = release_expired_holds(s, batch_size=10)
                if released == 0:
                    return total
                total += released

        counts = run_parallel(sweeper, list(range(4)))

        assert sum(counts) == n  # no hold released twice, none missed
        assert [status_of(i) for i in ids] == ["EXPIRED"] * n


def test_a_sweeper_skips_a_locked_booking_instead_of_waiting(pool_sessions):
    """SKIP LOCKED: while another transaction holds a booking, the sweep moves on
    rather than queueing behind it (lock_timeout turns a wait into a failure)."""
    with committed_world(n_users=2, n_seats=2) as w:
        locked_id = insert_held(w, w.user_ids[0], w.seat_ids[0], "locked")
        free_id = insert_held(w, w.user_ids[1], w.seat_ids[1], "free")

        holder = SessionLocal()
        holder.execute(select(Booking).where(Booking.id == locked_id).with_for_update())
        try:
            with pool_sessions() as s:
                s.execute(text("SET LOCAL lock_timeout = '3s'"))
                started = time.monotonic()
                released = release_expired_holds(s)
                elapsed = time.monotonic() - started
        finally:
            holder.rollback()
            holder.close()

        assert released == 1  # only the unlocked one
        assert elapsed < 2.0
        assert status_of(free_id) == "EXPIRED"
        assert status_of(locked_id) == "HELD"

        with pool_sessions() as s:
            assert release_expired_holds(s) == 1  # the next sweep gets it
        assert status_of(locked_id) == "EXPIRED"


class SlowGateway:
    """Sleeps, then charges. Counts calls."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.calls = 0

    def charge(self, key, amount):
        self.calls += 1
        time.sleep(self.seconds)
        return ChargeResult(ref="gw_slow")


def test_a_sweeper_never_releases_a_hold_while_its_payment_is_in_flight():
    """The hold expires WHILE the gateway call is running. Sweeping the whole time,
    the booking must stay HELD, then be confirmed when the payment lands."""
    with committed_world(n_users=1, n_seats=1) as w:
        uid = w.user_ids[0]
        with SessionLocal() as s:
            booking, _ = create_booking(
                s, user_id=uid, schedule_id=w.schedule_id, seat_id=w.seat_ids[0],
                idempotency_key="k", hold_seconds=2,
            )
        gateway = SlowGateway(seconds=3.0)

        def pay():
            with SessionLocal() as s:
                pay_booking(s, user_id=uid, booking_id=booking.id, idempotency_key="p",
                            gateway=gateway, breaker=CircuitBreaker(1000, 10), sleep=no_sleep)

        payer = threading.Thread(target=pay)
        payer.start()

        released_total = 0
        protected_observations = 0
        deadline = time.monotonic() + 4.5
        while time.monotonic() < deadline:
            with SessionLocal() as s:
                released_total += release_expired_holds(s)
            with SessionLocal() as s:
                b = s.get(Booking, booking.id)
                payment = s.scalar(select(Payment).where(Payment.booking_id == booking.id))
                if (payment is not None and payment.status == "PENDING"
                        and b.status == "HELD"
                        and b.hold_expires_at <= datetime.now(timezone.utc)):
                    protected_observations += 1
            time.sleep(0.25)
        payer.join(timeout=10)

        assert protected_observations >= 1, "never saw an expired hold with a payment in flight"
        assert released_total == 0
        with SessionLocal() as s:
            assert s.get(Booking, booking.id).status == "CONFIRMED"
            assert s.scalar(select(Payment.status).where(Payment.booking_id == booking.id)) == "SUCCESS"
        assert gateway.calls == 1


def test_parallel_redrive_workers_charge_each_stale_payment_exactly_once(pool_sessions, make_gateway):
    n = 10
    reliable = make_gateway(fail_rate=0, lost_response_rate=0, decline_rate=0)
    with committed_world(n_users=n, n_seats=n) as w:
        booking_ids = []
        with SessionLocal() as s:
            for i in range(n):
                b, _ = create_booking(s, user_id=w.user_ids[i], schedule_id=w.schedule_id,
                                      seat_id=w.seat_ids[i], idempotency_key=f"b{i}")
                booking_ids.append(b.id)
        # Every payment gets stuck PENDING: the first attempt only ever times out.
        for i in range(n):
            with SessionLocal() as s:
                pay_booking(s, user_id=w.user_ids[i], booking_id=booking_ids[i],
                            idempotency_key=f"p{i}",
                            gateway=ScriptedGateway(default=GatewayTimeout()),
                            breaker=CircuitBreaker(1000, 10), sleep=no_sleep, attempts=2)
        with SessionLocal() as s:
            s.execute(text("UPDATE payments SET updated_at = now() - interval '1 hour' "
                           "WHERE booking_id = ANY(:ids)"), {"ids": booking_ids})
            s.commit()
        assert reliable.ledger_entries() == []

        def worker(_):
            with pool_sessions() as s:
                return redrive_stale_payments(s, gateway=reliable, breaker=CircuitBreaker(1000, 10))

        stats = run_parallel(worker, list(range(4)))

        ledger = reliable.ledger_entries()
        assert len(ledger) == n  # one charge per payment, however many workers raced
        assert len({e["key"] for e in ledger}) == n
        assert sum(st.success for st in stats) == n
        assert sum(st.errors for st in stats) == 0
        with SessionLocal() as s:
            payments = s.scalars(select(Payment).where(Payment.booking_id.in_(booking_ids))).all()
            assert {p.status for p in payments} == {"SUCCESS"}
            assert {b.status for b in s.scalars(select(Booking).where(Booking.id.in_(booking_ids)))} == {"CONFIRMED"}
