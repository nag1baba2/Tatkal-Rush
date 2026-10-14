"""Idempotent demo data: one train, 3 upcoming schedules, 72 seats each.

Run:  docker compose exec api python -m app.seed
"""

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Schedule, Seat, Train

COACHES = ["S1", "S2", "S3", "S4"]
SEATS_PER_COACH = 18
FARE_PAISE = 85000  # Rs 850.00


def seed() -> None:
    with SessionLocal() as db:
        train = db.scalar(select(Train).where(Train.number == "12951"))
        if train is None:
            train = Train(
                number="12951",
                name="Tatkal Express",
                source="New Delhi",
                destination="Mumbai Central",
            )
            db.add(train)
            db.flush()

        for offset in (1, 2, 3):
            travel_date = date.today() + timedelta(days=offset)
            schedule = db.scalar(
                select(Schedule).where(
                    Schedule.train_id == train.id,
                    Schedule.travel_date == travel_date,
                )
            )
            if schedule is not None:
                continue
            schedule = Schedule(
                train_id=train.id,
                travel_date=travel_date,
                departs_at=datetime.combine(
                    travel_date, time(16, 35), tzinfo=timezone.utc
                ),
                fare_paise=FARE_PAISE,
            )
            db.add(schedule)
            db.flush()
            db.add_all(
                Seat(schedule_id=schedule.id, coach=coach, seat_no=n)
                for coach in COACHES
                for n in range(1, SEATS_PER_COACH + 1)
            )
        db.commit()


if __name__ == "__main__":
    seed()
    print("seeded")
