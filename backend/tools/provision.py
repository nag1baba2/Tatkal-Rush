"""Create an isolated load-test world: one train, one schedule with N hot seats, and
M users that already have JWTs (so a 1,000-user run does not spend its time on
bcrypt), written to a JSON file the Locust container reads.

Everything is tagged, so a run can be cleaned up exactly (tools.cleanup).
Starting a run also clears the gateway's ledger and the rate limiter's buckets, so
the numbers afterwards belong to this run only.

Run:  docker compose exec api python -m tools.provision --name mytest --users 1000 --seats 40
"""

import argparse
import json
import os
import pathlib
from datetime import date, datetime, timezone
from uuid import uuid4

import bcrypt
import redis
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth import create_access_token
from app.config import settings
from app.db import SessionLocal
from app.models import Booking, Payment, Schedule, Seat, Train, User
from app.services.payment import default_gateway

EMAIL_DOMAIN = "loadtest.test"
TRAVEL_DATE = date(2041, 1, 1)


def reset_shared_state() -> None:
    """Start from an empty gateway ledger and fresh rate-limit buckets."""
    default_gateway().reset()
    client = redis.Redis.from_url(settings.redis_url)
    for key in client.scan_iter(match="rl:*"):
        client.delete(key)


def create_run(name: str, users: int, seats: int, *, rounds: int = 1, db: Session | None = None) -> dict:
    tag = f"{name}-{uuid4().hex[:6]}"
    owns_session = db is None
    db = db or SessionLocal()
    try:
        train = Train(
            number=f"LT{uuid4().hex[:8]}",
            name="Load Test Express",
            source="Load Source",
            destination="Load Destination",
        )
        db.add(train)
        db.flush()
        schedule = Schedule(
            train_id=train.id,
            travel_date=TRAVEL_DATE,
            departs_at=datetime.combine(TRAVEL_DATE, datetime.min.time(), tzinfo=timezone.utc),
            fare_paise=85000,
        )
        db.add(schedule)
        db.flush()
        seat_rows = [Seat(schedule_id=schedule.id, coach="L1", seat_no=i + 1) for i in range(seats)]
        db.add_all(seat_rows)

        # One shared hash: nobody logs in with a password here, they use ready-made tokens.
        password_hash = bcrypt.hashpw(b"not-used", bcrypt.gensalt(rounds=4)).decode()
        user_rows = [
            User(email=f"{tag}-{i}@{EMAIL_DOMAIN}", password_hash=password_hash)
            for i in range(users)
        ]
        db.add_all(user_rows)
        db.commit()

        return {
            "name": name,
            "tag": tag,
            "rounds": rounds,
            "train_id": train.id,
            "schedule_id": schedule.id,
            "fare_paise": schedule.fare_paise,
            "seat_ids": [s.id for s in seat_rows],
            "users": [
                {"id": u.id, "token": create_access_token(u.id, expires_minutes=240)}
                for u in user_rows
            ],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "environment": {
                # Passed in by loadtest/run_scenario.sh (these live on the host, not in the container).
                "uvicorn_args": os.environ.get("UVICORN_ARGS", "unknown"),
                "api_workers": os.environ.get("API_WORKERS", "unknown"),
                "rate_limiter": os.environ.get("LIMITER_MODE", "unknown"),
                "hold_seconds": settings.hold_seconds,
                "payment_stale_seconds": settings.payment_stale_seconds,
                "gateway_fail_rate": settings.gateway_fail_rate,
                "gateway_lost_response_rate": settings.gateway_lost_response_rate,
                "gateway_decline_rate": settings.gateway_decline_rate,
                "gateway_latency_ms": settings.gateway_latency_ms,
                "retry_max_attempts": settings.retry_max_attempts,
                "rate_limit_enabled": settings.rate_limit_enabled,
                "rate_limit_identity": f"{settings.rate_limit_capacity} burst / {settings.rate_limit_refill_per_second} per s",
                "rate_limit_ip": f"{settings.rate_limit_ip_capacity} burst / {settings.rate_limit_ip_refill_per_second} per s",
                "db_pool": "20 + 20 overflow per API process",
            },
        }
    finally:
        if owns_session:
            db.close()


def delete_run(run: dict, db: Session | None = None) -> dict:
    """Remove exactly what create_run made (ids come from the run file). Returns row counts."""
    owns_session = db is None
    db = db or SessionLocal()
    try:
        schedule_id = run["schedule_id"]
        booking_ids = select(Booking.id).where(Booking.schedule_id == schedule_id)
        user_ids = [u["id"] for u in run["users"]]
        counts = {
            "payments": db.execute(delete(Payment).where(Payment.booking_id.in_(booking_ids))).rowcount,
            "bookings": db.execute(delete(Booking).where(Booking.schedule_id == schedule_id)).rowcount,
            "seats": db.execute(delete(Seat).where(Seat.schedule_id == schedule_id)).rowcount,
            "schedules": db.execute(delete(Schedule).where(Schedule.id == schedule_id)).rowcount,
            "trains": db.execute(delete(Train).where(Train.id == run["train_id"])).rowcount,
            "users": db.execute(delete(User).where(User.id.in_(user_ids))).rowcount,
        }
        db.commit()
        return counts
    finally:
        if owns_session:
            db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="run")
    parser.add_argument("--users", type=int, required=True)
    parser.add_argument("--seats", type=int, default=40)
    parser.add_argument("--rounds", type=int, default=1, help="bookings each user makes, one after another")
    parser.add_argument("--out", default="/loadtest-data/run.json")
    args = parser.parse_args()

    reset_shared_state()
    run = create_run(args.name, args.users, args.seats, rounds=args.rounds)
    path = pathlib.Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run))
    print(f"provisioned '{run['tag']}': schedule {run['schedule_id']}, {len(run['seat_ids'])} seats, "
          f"{len(run['users'])} users x {run['rounds']} round(s) -> {path}")


if __name__ == "__main__":
    main()
