"""Thin query helpers shared by rules/, api/ and journal/. No business logic
beyond basic CRUD and lookups -- guardrail decisions live in rules/guardrails.py."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from optionguard.db.models import (
    Check,
    CorporateEvent,
    KiteSession,
    Override,
    Settings,
    StopChange,
    Trade,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---- settings ----------------------------------------------------------

def get_active_settings(db: Session, at: datetime | None = None) -> Settings | None:
    at = at or utcnow()
    stmt = (
        select(Settings)
        .where(Settings.effective_from <= at)
        .order_by(Settings.effective_from.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def insert_settings(db: Session, *, effective_from: datetime, **fields) -> Settings:
    row = Settings(effective_from=effective_from, created_at=utcnow(), **fields)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def ensure_default_settings(db: Session, config) -> Settings:
    """Seeds the first settings row from config.toml [defaults] if none exists."""
    active = get_active_settings(db)
    if active is not None:
        return active
    return insert_settings(
        db,
        effective_from=utcnow(),
        max_loss_per_trade=config.defaults.max_loss_per_trade,
        monthly_loss_limit=config.defaults.monthly_loss_limit,
        premium_stop_pct=config.defaults.premium_stop_pct,
        time_stop_days=config.defaults.time_stop_days,
        cooloff_days=config.defaults.cooloff_days,
        show_spreads=False,
    )


# ---- checks --------------------------------------------------------------

def create_check(db: Session, **fields) -> Check:
    row = Check(created_at=utcnow(), **fields)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_check(db: Session, check_id: int) -> Check | None:
    return db.get(Check, check_id)


def list_checks(
    db: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    verdict: str | None = None,
    traded: bool | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[Check]:
    stmt = select(Check)
    if date_from:
        stmt = stmt.where(Check.created_at >= date_from)
    if date_to:
        stmt = stmt.where(Check.created_at <= date_to)
    if verdict:
        stmt = stmt.where(Check.verdict == verdict)
    if traded is not None:
        traded_ids = select(Trade.check_id)
        stmt = stmt.where(Check.id.in_(traded_ids) if traded else Check.id.not_in(traded_ids))
    stmt = stmt.order_by(Check.created_at.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all())


def checks_pending_settlement(db: Session, as_of: date) -> list[Check]:
    stmt = select(Check).where(Check.expiry < as_of, Check.expiry_settle.is_(None))
    return list(db.execute(stmt).scalars().all())


# ---- trades ----------------------------------------------------------

def create_trade(db: Session, **fields) -> Trade:
    row = Trade(status="open", **fields)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_trade(db: Session, trade_id: int) -> Trade | None:
    return db.get(Trade, trade_id)


def list_open_trades(db: Session) -> list[Trade]:
    stmt = select(Trade).where(Trade.status == "open").order_by(Trade.entry_time.desc())
    return list(db.execute(stmt).scalars().all())


def list_trades_closed_between(db: Session, start: datetime, end: datetime) -> list[Trade]:
    stmt = select(Trade).where(
        Trade.status == "closed",
        Trade.exit_time >= start,
        Trade.exit_time < end,
    )
    return list(db.execute(stmt).scalars().all())


def list_trades_all(db: Session, *, date_from: date | None = None, date_to: date | None = None) -> list[Trade]:
    stmt = select(Trade)
    if date_from:
        stmt = stmt.where(Trade.entry_time >= date_from)
    if date_to:
        stmt = stmt.where(Trade.entry_time <= date_to)
    return list(db.execute(stmt).scalars().all())


def add_stop_change(db: Session, **fields) -> StopChange:
    row = StopChange(at=utcnow(), **fields)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---- overrides ---------------------------------------------------------

def add_override(db: Session, **fields) -> Override:
    row = Override(at=utcnow(), **fields)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---- corporate events ----------------------------------------------------

def add_event(db: Session, **fields) -> CorporateEvent:
    row = CorporateEvent(**fields)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_events(db: Session, symbol: str | None = None) -> list[CorporateEvent]:
    stmt = select(CorporateEvent)
    if symbol:
        stmt = stmt.where(CorporateEvent.symbol == symbol.upper())
    stmt = stmt.order_by(CorporateEvent.event_date.asc())
    return list(db.execute(stmt).scalars().all())


def events_before(db: Session, symbol: str, before: date) -> list[CorporateEvent]:
    stmt = select(CorporateEvent).where(
        CorporateEvent.symbol == symbol.upper(), CorporateEvent.event_date <= before
    )
    return list(db.execute(stmt).scalars().all())


# ---- kite session ------------------------------------------------------

def get_kite_session(db: Session) -> KiteSession | None:
    return db.get(KiteSession, 1)


def set_kite_session(db: Session, access_token: str, valid_for: date) -> KiteSession:
    row = db.get(KiteSession, 1)
    if row is None:
        row = KiteSession(id=1, access_token=access_token, valid_for=valid_for)
        db.add(row)
    else:
        row.access_token = access_token
        row.valid_for = valid_for
    db.commit()
    db.refresh(row)
    return row
