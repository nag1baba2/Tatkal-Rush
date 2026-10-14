"""Helpers for concurrency tests, which need COMMITTED data visible to many
independent connections (so the usual rolled-back fixture cannot be used).
Everything created here is deleted again in the `finally` block.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, select, text

from app.circuit_breaker import CircuitBreaker
from app.db import SessionLocal
from app.gateway import ChargeResult
from app.models import Booking, Payment, Schedule, Seat, Train, User
from app.services.booking import create_booking
from app.services.payment import pay_booking


@dataclass
class World:
    user_ids: list[int]
    seat_ids: list[int]
    schedule_id: int
    train_id: int = 0


@contextmanager
def committed_world(n_users: int, n_seats: int):
    tag = uuid4().hex[:8]
    with SessionLocal() as s:
        train = Train(number=f"C{tag}", name="Concurrency Test", source="A", destination="B")
        users = [
            User(email=f"{tag}-{i}@concurrency.test", password_hash="x")
            for i in range(n_users)
        ]
        s.add(train)
        s.add_all(users)
        s.flush()
        schedule = Schedule(
            train_id=train.id,
            travel_date=date(2040, 1, 1),
            departs_at=datetime(2040, 1, 1, 10, 0, tzinfo=timezone.utc),
            fare_paise=50000,
        )
        s.add(schedule)
        s.flush()
        seats = [
            Seat(schedule_id=schedule.id, coach="S1", seat_no=i + 1)
            for i in range(n_seats)
        ]
        s.add_all(seats)
        s.commit()
        world = World(
            user_ids=[u.id for u in users],
            seat_ids=[x.id for x in seats],
            schedule_id=schedule.id,
            train_id=train.id,
        )
        train_id = train.id
    try:
        yield world
    finally:
        with SessionLocal() as s:
            booking_ids = select(Booking.id).where(Booking.schedule_id == world.schedule_id)
            s.execute(delete(Payment).where(Payment.booking_id.in_(booking_ids)))
            s.execute(delete(Booking).where(Booking.schedule_id == world.schedule_id))
            s.execute(delete(Seat).where(Seat.schedule_id == world.schedule_id))
            s.execute(delete(Schedule).where(Schedule.id == world.schedule_id))
            s.execute(delete(Train).where(Train.id == train_id))
            s.execute(delete(User).where(User.id.in_(world.user_ids)))
            s.commit()


def active_booking_count(seat_id: int) -> int:
    with SessionLocal() as s:
        return len(
            s.scalars(
                select(Booking.id).where(
                    Booking.seat_id == seat_id,
                    Booking.status.in_(("HELD", "CONFIRMED")),
                )
            ).all()
        )


def run_parallel(fn, args):
    """Run fn(arg) for every arg, releasing all threads at the same instant."""
    barrier = threading.Barrier(len(args))

    def wrapped(arg):
        barrier.wait()
        return fn(arg)

    with ThreadPoolExecutor(max_workers=len(args)) as ex:
        return list(ex.map(wrapped, args))


def no_sleep(_seconds):
    """Stands in for time.sleep so retry/backoff tests run instantly."""


class ScriptedGateway:
    """A gateway that plays back a fixed script: each call pops the next item.
    An Exception is raised; a string is returned as the charge reference. When
    the script runs out it uses `default` (None means: fail the test, because
    the gateway must not have been called)."""

    def __init__(self, *script, default=None):
        self.script = list(script)
        self.default = default
        self.calls: list[tuple[str, int]] = []

    def charge(self, idempotency_key, amount_paise):
        self.calls.append((idempotency_key, amount_paise))
        item = self.script.pop(0) if self.script else self.default
        if item is None:
            raise AssertionError("gateway was called but should not have been")
        if isinstance(item, Exception):
            raise item
        return ChargeResult(ref=item)


# ---- helpers for tests that use the rolled-back `db` + `world` fixtures ----

def new_breaker(threshold=1000):
    return CircuitBreaker(failure_threshold=threshold, recovery_timeout=10)


def hold(db, world, *, user="u1", seat="seat1", sched="s1", key="b1", seconds=60):
    booking, _ = create_booking(
        db,
        user_id=world[user].id,
        schedule_id=world[sched].id,
        seat_id=world[seat].id,
        idempotency_key=key,
        hold_seconds=seconds,
    )
    return booking


def pay(db, world, booking, gateway, *, key="p1", user="u1", breaker=None, **kw):
    kw.setdefault("attempts", 4)
    kw.setdefault("stale_seconds", 30)
    return pay_booking(
        db,
        user_id=world[user].id,
        booking_id=booking.id,
        idempotency_key=key,
        gateway=gateway,
        breaker=breaker or new_breaker(),
        sleep=no_sleep,
        rng=lambda: 0.0,
        **kw,
    )


def make_stale(db):
    db.execute(text("UPDATE payments SET updated_at = now() - interval '1 hour'"))
    db.commit()
    db.expire_all()


def expire_hold(db, booking):
    db.execute(
        text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute' WHERE id = :i"),
        {"i": booking.id},
    )
    db.commit()
    db.expire_all()
