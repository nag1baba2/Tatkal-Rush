"""Delete load-test data.

  python -m tools.cleanup --run /loadtest-data/run.json     one run (exact ids from the file)
  python -m tools.cleanup --all                             every load-test train/user ever made
"""

import argparse
import json
import pathlib

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import Booking, Payment, Schedule, Seat, Train, User
from tools.provision import EMAIL_DOMAIN, delete_run


def delete_everything() -> dict:
    with SessionLocal() as db:
        train_ids = db.scalars(select(Train.id).where(Train.name == "Load Test Express")).all()
        schedule_ids = db.scalars(select(Schedule.id).where(Schedule.train_id.in_(train_ids))).all()
        bookings = select(Booking.id).where(Booking.schedule_id.in_(schedule_ids))
        counts = {
            "payments": db.execute(delete(Payment).where(Payment.booking_id.in_(bookings))).rowcount,
            "bookings": db.execute(delete(Booking).where(Booking.schedule_id.in_(schedule_ids))).rowcount,
            "seats": db.execute(delete(Seat).where(Seat.schedule_id.in_(schedule_ids))).rowcount,
            "schedules": db.execute(delete(Schedule).where(Schedule.id.in_(schedule_ids))).rowcount,
            "trains": db.execute(delete(Train).where(Train.id.in_(train_ids))).rowcount,
            "users": db.execute(delete(User).where(User.email.like(f"%@{EMAIL_DOMAIN}"))).rowcount,
        }
        db.commit()
        return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run")
    group.add_argument("--all", action="store_true")
    args = parser.parse_args()
    counts = delete_everything() if args.all else delete_run(json.loads(pathlib.Path(args.run).read_text()))
    print("deleted:", counts)


if __name__ == "__main__":
    main()
