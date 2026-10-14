from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Schedule
from app.schemas import SeatOut
from app.services.availability import seat_states

router = APIRouter(prefix="/schedules", tags=["schedules"])


@router.get("/{schedule_id}/seats", response_model=list[SeatOut])
def seat_map(schedule_id: int, db: Session = Depends(get_db)):
    """Seat availability, consistent with what a booking attempt would decide."""
    if db.get(Schedule, schedule_id) is None:
        raise HTTPException(status_code=404, detail="schedule_not_found")
    states = seat_states(db, schedule_id)
    db.commit()  # end the transaction now: a connection must never be held while this request waits for a thread
    return [
        SeatOut(id=seat.id, coach=seat.coach, seat_no=seat.seat_no, status=label)
        for seat, label in states
    ]
