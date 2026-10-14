from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Schedule, Train
from app.schemas import ScheduleOut, TrainOut
from app.services.availability import seat_states

router = APIRouter(prefix="/trains", tags=["trains"])


def _contains(column, text: str):
    """Case-insensitive 'contains' that treats % and _ in the input literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.ilike(f"%{escaped}%", escape="\\")


@router.get("", response_model=list[TrainOut])
def search_trains(
    source: str | None = Query(default=None, max_length=100),
    destination: str | None = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
):
    query = select(Train).order_by(Train.number).limit(50)
    if source and source.strip():
        query = query.where(_contains(Train.source, source.strip()))
    if destination and destination.strip():
        query = query.where(_contains(Train.destination, destination.strip()))
    trains = db.scalars(query).all()
    db.commit()  # end the transaction now: a connection must never be held while this request waits for a thread
    return trains


@router.get("/{train_id}/schedules", response_model=list[ScheduleOut])
def upcoming_schedules(train_id: int, db: Session = Depends(get_db)):
    """Upcoming runs of a train with how many seats are still available."""
    if db.get(Train, train_id) is None:
        raise HTTPException(status_code=404, detail="train_not_found")

    today = datetime.now(timezone.utc).date()
    schedules = db.scalars(
        select(Schedule)
        .where(Schedule.train_id == train_id, Schedule.travel_date >= today)
        .order_by(Schedule.travel_date)
    ).all()

    out = []
    for schedule in schedules:
        states = seat_states(db, schedule.id)
        out.append(
            ScheduleOut(
                id=schedule.id,
                train_id=schedule.train_id,
                travel_date=schedule.travel_date,
                departs_at=schedule.departs_at,
                fare_paise=schedule.fare_paise,
                seats_total=len(states),
                seats_available=sum(1 for _, label in states if label == "AVAILABLE"),
            )
        )
    db.commit()  # end the transaction now: a connection must never be held while this request waits for a thread
    return out
