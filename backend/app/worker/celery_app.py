"""Celery app and schedule.

  worker:  celery -A app.worker.celery_app:celery_app worker
  beat:    celery -A app.worker.celery_app:celery_app beat

Beat (the scheduler) puts each sweep on the queue every EXPIRY_SWEEP_SECONDS;
workers run them. Delivery is "at least once" (acks_late), which is safe because
every sweep is idempotent.
"""

from celery import Celery

from app.config import settings


def broker_url() -> str:
    if settings.celery_broker_url:
        return settings.celery_broker_url
    # Same Redis server, database 1 (the gateway's keys live in database 0).
    return settings.redis_url.rsplit("/", 1)[0] + "/1"


celery_app = Celery("tatkal", broker=broker_url(), include=["app.worker.tasks"])

_interval = float(settings.expiry_sweep_seconds)

celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
    # Run a task only after it finishes, and put it back if the worker dies
    # mid-task. Sweeps are idempotent, so running one twice is harmless.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "release-expired-holds": {
            "task": "app.worker.tasks.sweep_expired_holds",
            "schedule": _interval,
            # If every worker is down, don't pile up a backlog of stale sweeps.
            "options": {"expires": _interval * 3},
        },
        "redrive-stale-payments": {
            "task": "app.worker.tasks.redrive_stale_payments",
            "schedule": _interval,
            "options": {"expires": _interval * 3},
        },
    },
)
