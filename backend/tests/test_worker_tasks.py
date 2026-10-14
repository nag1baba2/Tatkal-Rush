"""Celery wiring: schedule, retry policy, pause flag, and the task bodies."""

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.db import SessionLocal
from app.models import Booking
from app.services.booking import create_booking
from app.worker import tasks
from app.worker.celery_app import broker_url, celery_app
from app.worker.control import pause_sweeps, resume_sweeps, sweeps_paused
from tests.helpers import committed_world

TASK_NAMES = {
    "app.worker.tasks.sweep_expired_holds",
    "app.worker.tasks.redrive_stale_payments",
}


def test_both_tasks_are_registered():
    assert TASK_NAMES <= set(celery_app.tasks)


def test_beat_schedules_both_sweeps_every_interval_with_an_expiry():
    schedule = celery_app.conf.beat_schedule
    assert {entry["task"] for entry in schedule.values()} == TASK_NAMES
    for entry in schedule.values():
        assert entry["schedule"] == float(settings.expiry_sweep_seconds)
        # a stale queued sweep is dropped rather than piling up behind a dead worker
        assert entry["options"]["expires"] == entry["schedule"] * 3


def test_the_default_sweep_interval_is_ten_seconds():
    # compose passes EXPIRY_SWEEP_SECONDS (default 10); the code default matches
    assert settings.expiry_sweep_seconds == 10


def test_tasks_retry_transient_infrastructure_errors_with_backoff():
    for name in TASK_NAMES:
        task = celery_app.tasks[name]
        assert OperationalError in task.autoretry_for
        assert task.max_retries == 3
        assert task.retry_backoff is True
        assert task.retry_jitter is True


def test_tasks_acknowledge_late_so_a_dead_worker_does_not_lose_a_sweep():
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True


def test_the_broker_uses_a_different_redis_database_from_the_gateway():
    assert broker_url().endswith("/1")
    assert settings.redis_url.endswith("/0")


# ---- the pause flag ------------------------------------------------------------------------

def test_pause_flag_round_trip():
    # the suite-wide fixture has paused the sweeps; restore that state afterwards
    try:
        resume_sweeps()
        assert sweeps_paused() is False
        pause_sweeps(60)
        assert sweeps_paused() is True
    finally:
        pause_sweeps(1800)


def test_the_pause_flag_expires_by_itself():
    from app.worker.control import PAUSE_KEY, _client

    try:
        pause_sweeps(60)
        assert 0 < _client().ttl(PAUSE_KEY) <= 60
    finally:
        pause_sweeps(1800)


def expired_hold(world):
    with SessionLocal() as s:
        booking, _ = create_booking(
            s, user_id=world.user_ids[0], schedule_id=world.schedule_id,
            seat_id=world.seat_ids[0], idempotency_key="k",
        )
        s.execute(
            text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute' WHERE id=:i"),
            {"i": booking.id},
        )
        s.commit()
        return booking.id


def status_of(booking_id):
    with SessionLocal() as s:
        return s.get(Booking, booking_id).status


def test_paused_tasks_do_nothing():
    with committed_world(n_users=1, n_seats=1) as w:
        bid = expired_hold(w)
        assert sweeps_paused()  # the suite-wide fixture
        assert tasks.sweep_expired_holds.apply().result == 0
        assert tasks.redrive_stale_payments.apply().result == {"paused": True}
        assert status_of(bid) == "HELD"


def test_the_hold_sweep_body_expires_overdue_holds_in_the_real_database():
    with committed_world(n_users=3, n_seats=3) as w:
        ids = []
        with SessionLocal() as s:
            for i in range(3):
                b, _ = create_booking(
                    s, user_id=w.user_ids[i], schedule_id=w.schedule_id,
                    seat_id=w.seat_ids[i], idempotency_key=f"k{i}",
                )
                ids.append(b.id)
            s.execute(
                text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute' "
                     "WHERE id = ANY(:ids)"), {"ids": ids})
            s.commit()
        assert tasks.run_hold_sweep() == 3
        assert [status_of(i) for i in ids] == ["EXPIRED"] * 3
        assert tasks.run_hold_sweep() == 0


def test_the_hold_sweep_body_works_through_batches(monkeypatch):
    monkeypatch.setattr(settings, "sweep_batch_size", 2)
    with committed_world(n_users=5, n_seats=5) as w:
        with SessionLocal() as s:
            for i in range(5):
                create_booking(s, user_id=w.user_ids[i], schedule_id=w.schedule_id,
                               seat_id=w.seat_ids[i], idempotency_key=f"k{i}")
            s.execute(text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute' "
                           "WHERE schedule_id = :s"), {"s": w.schedule_id})
            s.commit()
        assert tasks.run_hold_sweep() == 5  # 2 + 2 + 1, across several batches
