"""GET /journal/stats, GET /journal/checks. Per DESIGN.md sections 11 and 17 (M7)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from optionguard.api.errors import ApiError
from optionguard.api.serializers import check_to_dict
from optionguard.db import repo
from optionguard.db.session import get_db
from optionguard.journal.stats import compute_stats

router = APIRouter(prefix="/api/v1/journal", tags=["journal"])


def _to_datetime(d: date | None) -> datetime | None:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) if d else None


@router.get("/stats")
def get_journal_stats(
    from_: date | None = Query(None, alias="from"),
    to: date | None = None,
    group_by: str = "verdict",
    db: Session = Depends(get_db),
):
    try:
        return compute_stats(db, date_from=_to_datetime(from_), date_to=_to_datetime(to), group_by=group_by)
    except ValueError as exc:
        raise ApiError("BAD_GROUP_BY", str(exc)) from exc


@router.get("/checks")
def get_journal_checks(
    from_: date | None = Query(None, alias="from"),
    to: date | None = None,
    verdict: str | None = None,
    traded: bool | None = None,
    acted: bool | None = None,
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    rows = repo.list_checks(
        db, date_from=from_, date_to=to, verdict=verdict, traded=traded, acted=acted, limit=limit, offset=offset
    )
    return [check_to_dict(r) for r in rows]
