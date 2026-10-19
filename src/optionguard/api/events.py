"""GET/POST /events -- corporate events (earnings, dividends). Per
DESIGN.md section 11."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from optionguard.api.schemas import EventIn
from optionguard.api.serializers import event_to_dict
from optionguard.db import repo
from optionguard.db.session import get_db

router = APIRouter(prefix="/api/v1", tags=["events"])


@router.get("/events")
def get_events(symbol: str | None = None, db: Session = Depends(get_db)):
    return [event_to_dict(e) for e in repo.list_events(db, symbol)]


@router.post("/events")
def post_event(body: EventIn, db: Session = Depends(get_db)):
    row = repo.add_event(
        db, symbol=body.symbol.upper(), event_type=body.event_type, event_date=body.event_date, dividend=body.dividend
    )
    return event_to_dict(row)
