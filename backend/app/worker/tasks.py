"""Celery tasks: thin wrappers that open a session and call the service functions.

Retries: if the database or Redis is briefly unavailable the task is retried up to
3 times with exponential backoff and jitter (1s, 2s, 4s ceiling, capped at 30s).
Business errors are not retried here: they are handled inside the sweep, and
anything left over is simply picked up by the next scheduled run.

The real work is in run_hold_sweep / run_payment_redrive (no Celery, no pause
check) so tests can call it directly; the tasks add the pause flag on top.
"""

import logging
from datetime import datetime, timezone

import redis.exceptions
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.db import SessionLocal
from app.models import Payment
from app.services.expiry import redrive_stale_payments as redrive_service
from app.services.expiry import release_expired_holds
from app.worker.celery_app import celery_app
from app.worker.control import sweeps_paused

log = logging.getLogger(__name__)

TRANSIENT_ERRORS = (OperationalError, redis.exceptions.ConnectionError)
RETRY_POLICY = dict(
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=30,
    retry_jitter=True,
    max_retries=3,
)


def run_hold_sweep() -> int:
    """Expire overdue holds, in batches. Returns how many were released."""
    total = 0
    with SessionLocal() as db:
        while True:
            released = release_expired_holds(db, batch_size=settings.sweep_batch_size)
            total += released
            if released < settings.sweep_batch_size:
                break
    return total


def run_payment_redrive() -> dict:
    """Resolve PENDING payments nobody has touched for a while."""
    with SessionLocal() as db:
        stats = redrive_service(db)
        oldest = db.scalar(
            select(func.min(Payment.updated_at)).where(Payment.status == "PENDING")
        )
    if oldest is not None:
        age = (datetime.now(timezone.utc) - oldest).total_seconds()
        if age > settings.payment_alert_seconds:
            # In production this would page someone: money may be unaccounted for,
            # and the seat stays held until the payment is resolved.
            log.warning("a payment has been unresolved for %.0f s", age)
    return vars(stats)


@celery_app.task(name="app.worker.tasks.sweep_expired_holds", **RETRY_POLICY)
def sweep_expired_holds() -> int:
    if sweeps_paused():
        return 0
    released = run_hold_sweep()
    if released:
        log.info("released %s expired hold(s)", released)
    return released


@celery_app.task(name="app.worker.tasks.redrive_stale_payments", **RETRY_POLICY)
def redrive_stale_payments() -> dict:
    if sweeps_paused():
        return {"paused": True}
    stats = run_payment_redrive()
    if any(stats.values()):
        log.info("payment re-drive: %s", stats)
    return stats
