"""Thin query helpers shared by rules/, api/ and journal/. No business logic
beyond basic CRUD and lookups -- guardrail decisions live in rules/guardrails.py."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from optionguard.db.models import (
    Check,
    CorporateEvent,
    KiteSession,
    OptionMinuteBar,
    OptionThesis,
    OptionThesisPolicyResult,
    OptionThesisScore,
    OrderIntent,
    Override,
    Settings,
    StopChange,
    SyncRun,
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


def set_of_referenced_check_ids(db: Session) -> set[int]:
    """Every check_id touched by a trade, option thesis, order-ticket intent
    (even an abandoned one), or override -- the full set of "acted on", used
    both to gate manual check deletion (FK safety) and to flag which rows
    in the journal's "all checks" view are deletable."""
    ids: set[int] = set()
    for model in (Trade, OptionThesis, OrderIntent, Override):
        ids.update(cid for cid, in db.execute(select(model.check_id)) if cid is not None)
    return ids


def delete_check(db: Session, check_id: int) -> None:
    check = db.get(Check, check_id)
    if check is not None:
        db.delete(check)
        db.commit()


def list_checks(
    db: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    verdict: str | None = None,
    traded: bool | None = None,
    acted: bool | None = None,
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
    if acted is not None:
        # "Acted on" = led to a trade (any path) or an option thesis -- not just explored.
        acted_ids = select(Trade.check_id).union(select(OptionThesis.check_id))
        stmt = stmt.where(Check.id.in_(acted_ids) if acted else Check.id.not_in(acted_ids))
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


def list_closed_trades(db: Session) -> list[Trade]:
    stmt = select(Trade).where(Trade.status == "closed").order_by(Trade.exit_time.desc())
    return list(db.execute(stmt).scalars().all())


def get_open_trade_for_contract(db: Session, symbol: str, expiry: date, strike: float, opt_type: str) -> Trade | None:
    """Section 7.8.4: an entry ticket is refused if an open trade already
    exists on the same contract."""
    stmt = (
        select(Trade)
        .join(Check, Check.id == Trade.check_id)
        .where(
            Trade.status == "open", Check.symbol == symbol.upper(), Check.expiry == expiry,
            Check.strike == strike, Check.opt_type == opt_type.upper(),
        )
    )
    return db.execute(stmt).scalars().first()


def update_trade_order_fields(db: Session, trade_id: int, **fields) -> Trade:
    """Section 7.8: entry_intent_id/exit_intent_id/gtt_id/gtt_status --
    separate from the guardrail-sensitive stop fields, which keep going
    through tighten/loosen."""
    trade = db.get(Trade, trade_id)
    for key, value in fields.items():
        setattr(trade, key, value)
    db.commit()
    db.refresh(trade)
    return trade


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


def create_sync_run(db: Session, trigger: str) -> SyncRun:
    row = SyncRun(started_at=utcnow(), trigger=trigger, status="running")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def finish_sync_run(db: Session, run_id: int, *, status: str, counts: dict, errors: list) -> SyncRun:
    row = db.get(SyncRun, run_id)
    row.finished_at = utcnow()
    row.status = status
    row.counts = counts
    row.errors = errors
    db.commit()
    db.refresh(row)
    return row


def get_latest_sync_run(db: Session) -> SyncRun | None:
    stmt = select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)
    return db.execute(stmt).scalar_one_or_none()


def is_sync_running(db: Session) -> bool:
    stmt = select(SyncRun).where(SyncRun.status == "running").limit(1)
    return db.execute(stmt).scalar_one_or_none() is not None


# ---- Section 7.8: order ticket -------------------------------------------

LIVE_INTENT_STATUSES = ("reviewed", "placed")


def create_order_intent(db: Session, **fields) -> OrderIntent:
    row = OrderIntent(created_at=utcnow(), status="reviewed", **fields)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_order_intent(db: Session, intent_id: int) -> OrderIntent | None:
    return db.get(OrderIntent, intent_id)


def get_live_intent_for_contract(db: Session, tradingsymbol: str) -> OrderIntent | None:
    """7.8.4: no second live intent (reviewed or placed) on the same
    contract -- mirrors the partial unique index at the DB level."""
    stmt = select(OrderIntent).where(
        OrderIntent.tradingsymbol == tradingsymbol, OrderIntent.status.in_(LIVE_INTENT_STATUSES)
    )
    return db.execute(stmt).scalars().first()


def try_transition_order_intent(db: Session, intent_id: int, *, from_status: str, to_status: str, **fields) -> OrderIntent | None:
    """Idempotency (7.8.7): the reviewed -> placed transition (Confirm) is a
    conditional UPDATE ... WHERE status = from_status. A double-submit of
    the same intent_id only ever wins the race once -- the loser gets None
    back, never a second real order."""
    stmt = (
        update(OrderIntent)
        .where(OrderIntent.id == intent_id, OrderIntent.status == from_status)
        .values(status=to_status, **fields)
    )
    result = db.execute(stmt)
    db.commit()
    if result.rowcount == 0:
        return None
    return db.get(OrderIntent, intent_id)


def update_order_intent(db: Session, intent_id: int, **fields) -> OrderIntent:
    """Unconditional update -- for recording fill-poll progress on an intent
    that's already past the reviewed->placed transition."""
    row = db.get(OrderIntent, intent_id)
    for key, value in fields.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


def list_order_intents(db: Session, *, status: str | None = None, limit: int = 50) -> list[OrderIntent]:
    stmt = select(OrderIntent)
    if status:
        stmt = stmt.where(OrderIntent.status == status)
    stmt = stmt.order_by(OrderIntent.created_at.desc()).limit(limit)
    return list(db.execute(stmt).scalars().all())


def list_placed_order_intents(db: Session) -> list[OrderIntent]:
    """7.8.6: intents still 'placed' resume fill-tracking on the next page
    load or sync, since a fill-poll loop doesn't survive a process restart."""
    stmt = select(OrderIntent).where(OrderIntent.status == "placed")
    return list(db.execute(stmt).scalars().all())


def get_latest_order_attempt(db: Session) -> OrderIntent | None:
    """7.8.2: the most recent intent that actually reached Kite -- excludes
    'reviewed'/'expired', which never placed a real order. Drives GET
    /status's orders.ip_ok (null until the first real attempt)."""
    stmt = (
        select(OrderIntent)
        .where(OrderIntent.status.in_(("placed", "filled", "partial", "rejected", "cancelled")))
        .order_by(OrderIntent.created_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


# ---- Section 7.9: option theses --------------------------------------------

OPTION_THESIS_ALWAYS_MUTABLE = {"notes", "status"}
OPTION_THESIS_ONCE_FIELDS = {
    "outcome", "exit_time", "exit_premium", "manual_exit_reason", "manual_exit_entered_at",
    "trade_id", "cancelled_at", "cancel_reason",
}


def create_option_thesis(db: Session, **fields) -> OptionThesis:
    row = OptionThesis(created_at=utcnow(), status="open", **fields)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_option_thesis(db: Session, thesis_id: int) -> OptionThesis | None:
    return db.get(OptionThesis, thesis_id)


def get_open_option_thesis_for_contract(
    db: Session, symbol: str, expiry: date, strike: float, opt_type: str
) -> OptionThesis | None:
    stmt = select(OptionThesis).where(
        OptionThesis.symbol == symbol, OptionThesis.expiry == expiry,
        OptionThesis.strike == strike, OptionThesis.opt_type == opt_type, OptionThesis.status == "open",
    )
    return db.execute(stmt).scalar_one_or_none()


def list_option_theses(
    db: Session, *, status: str | None = None, mode: str | None = None, sector: str | None = None,
    symbol: str | None = None, verdict: str | None = None, date_from: date | None = None,
    date_to: date | None = None, limit: int = 200, offset: int = 0,
) -> list[OptionThesis]:
    stmt = select(OptionThesis)
    if status:
        stmt = stmt.where(OptionThesis.status == status)
    if mode:
        stmt = stmt.where(OptionThesis.mode == mode)
    if sector:
        stmt = stmt.where(OptionThesis.sector == sector)
    if symbol:
        stmt = stmt.where(OptionThesis.symbol == symbol.upper())
    if verdict:
        stmt = stmt.where(OptionThesis.verdict == verdict)
    if date_from:
        stmt = stmt.where(OptionThesis.created_at >= date_from)
    if date_to:
        stmt = stmt.where(OptionThesis.created_at <= date_to)
    stmt = stmt.order_by(OptionThesis.created_at.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all())


def update_option_thesis(db: Session, thesis_id: int, **fields) -> OptionThesis:
    """Repository-layer immutability enforcement (first line of defence --
    the Postgres trigger from the migration is the second), mirroring its
    exact rules: notes/status freely; outcome/exit_time/exit_premium/
    manual_exit_reason/manual_exit_entered_at/trade_id/cancelled_at/
    cancel_reason each settable once (from null); entry_premium only for a
    'real' thesis not yet linked to a trade."""
    row = db.get(OptionThesis, thesis_id)
    if row is None:
        raise ValueError(f"option thesis {thesis_id} not found")
    allowed = OPTION_THESIS_ALWAYS_MUTABLE | OPTION_THESIS_ONCE_FIELDS | {"entry_premium"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"option theses are immutable except {sorted(allowed)}; got {sorted(bad)}")
    for key in OPTION_THESIS_ONCE_FIELDS:
        if key in fields and getattr(row, key) is not None:
            raise ValueError(f"option_theses.{key} can only be set once")
    if "entry_premium" in fields and not (row.mode == "real" and row.trade_id is None):
        raise ValueError("option_theses.entry_premium cannot change (real: only once, before a trade is linked)")
    for key, value in fields.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


def list_option_theses_needing_bars(db: Session, as_of: date) -> list[OptionThesis]:
    """Non-mock theses with an unexpired contract, not yet scored or
    cancelled -- both paper and real need bars (7.9.7's policy replay needs
    them for real theses too)."""
    stmt = select(OptionThesis).where(
        OptionThesis.data_source != "mock", OptionThesis.expiry >= as_of,
        OptionThesis.opt_instrument_token.is_not(None), OptionThesis.status.in_(("open", "closed")),
    )
    return list(db.execute(stmt).scalars().all())


def list_option_theses_open(db: Session) -> list[OptionThesis]:
    """Non-mock, open theses -- candidates for resolution from bars (7.9.5)."""
    stmt = select(OptionThesis).where(OptionThesis.status == "open", OptionThesis.data_source != "mock")
    return list(db.execute(stmt).scalars().all())


def list_option_theses_due_for_scoring(db: Session, as_of: date) -> list[OptionThesis]:
    """Closed (outcome known), non-mock theses whose tracking window has
    ended -- ready to score (7.9.6 step 3)."""
    stmt = select(OptionThesis).where(
        OptionThesis.status == "closed", OptionThesis.data_source != "mock", OptionThesis.end_date <= as_of,
    )
    return list(db.execute(stmt).scalars().all())


def list_option_theses_for_policy_compute(db: Session) -> list[OptionThesis]:
    """Scored theses -- policy results (7.9.7) are (re)computed every run,
    not gated on end_date like the thesis's own outcome, since `hold` can
    stay pending well past end_date."""
    stmt = select(OptionThesis).where(OptionThesis.status == "scored", OptionThesis.data_source != "mock")
    return list(db.execute(stmt).scalars().all())


def list_option_theses_real_unlinked(db: Session, before: datetime) -> list[OptionThesis]:
    """Real theses created before `before` with no linked trade yet --
    auto-cancel candidates (7.9.5: "no trade linked" by end of entry day)."""
    stmt = select(OptionThesis).where(
        OptionThesis.mode == "real", OptionThesis.trade_id.is_(None),
        OptionThesis.status == "open", OptionThesis.created_at < before,
    )
    return list(db.execute(stmt).scalars().all())


def list_scored_option_theses_with_scores(
    db: Session, *, mode: str | None = None, date_from: date | None = None, date_to: date | None = None,
) -> list[tuple[OptionThesis, OptionThesisScore]]:
    """Scorecard inputs (7.9.8): excludes mock and cancelled theses."""
    stmt = (
        select(OptionThesis, OptionThesisScore)
        .join(OptionThesisScore, OptionThesisScore.thesis_id == OptionThesis.id)
        .where(OptionThesis.status == "scored", OptionThesis.data_source != "mock")
    )
    if mode and mode != "all":
        stmt = stmt.where(OptionThesis.mode == mode)
    if date_from:
        stmt = stmt.where(OptionThesis.created_at >= date_from)
    if date_to:
        stmt = stmt.where(OptionThesis.created_at <= date_to)
    return [(row[0], row[1]) for row in db.execute(stmt).all()]


def count_cancelled_option_theses(
    db: Session, *, mode: str | None = None, date_from: date | None = None, date_to: date | None = None,
) -> int:
    stmt = select(OptionThesis).where(OptionThesis.status == "cancelled", OptionThesis.data_source != "mock")
    if mode and mode != "all":
        stmt = stmt.where(OptionThesis.mode == mode)
    if date_from:
        stmt = stmt.where(OptionThesis.created_at >= date_from)
    if date_to:
        stmt = stmt.where(OptionThesis.created_at <= date_to)
    return len(db.execute(stmt).scalars().all())


def upsert_option_minute_bar(db: Session, *, instrument_token: int, ts: datetime, **fields) -> OptionMinuteBar:
    row = db.get(OptionMinuteBar, (instrument_token, ts))
    if row is None:
        row = OptionMinuteBar(instrument_token=instrument_token, ts=ts, fetched_at=utcnow(), **fields)
        db.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
        row.fetched_at = utcnow()
    db.commit()
    return row


def list_option_minute_bars(
    db: Session, instrument_token: int, *, from_ts: datetime | None = None, to_ts: datetime | None = None,
) -> list[OptionMinuteBar]:
    stmt = select(OptionMinuteBar).where(OptionMinuteBar.instrument_token == instrument_token)
    if from_ts:
        stmt = stmt.where(OptionMinuteBar.ts >= from_ts)
    if to_ts:
        stmt = stmt.where(OptionMinuteBar.ts <= to_ts)
    stmt = stmt.order_by(OptionMinuteBar.ts)
    return list(db.execute(stmt).scalars().all())


def get_option_thesis_score(db: Session, thesis_id: int) -> OptionThesisScore | None:
    return db.get(OptionThesisScore, thesis_id)


def upsert_option_thesis_score(db: Session, thesis_id: int, **fields) -> OptionThesisScore:
    """Rescoring replaces this row -- never edits the linked OptionThesis."""
    row = db.get(OptionThesisScore, thesis_id)
    if row is None:
        row = OptionThesisScore(thesis_id=thesis_id, **fields)
        db.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


def list_option_thesis_policy_results(db: Session, thesis_id: int) -> list[OptionThesisPolicyResult]:
    stmt = select(OptionThesisPolicyResult).where(OptionThesisPolicyResult.thesis_id == thesis_id)
    return list(db.execute(stmt).scalars().all())


def list_option_thesis_policy_results_for_theses(db: Session, thesis_ids: list[int]) -> list[OptionThesisPolicyResult]:
    """Bulk fetch for the scorecard's policy block (7.9.8) -- one query
    instead of one per thesis."""
    if not thesis_ids:
        return []
    stmt = select(OptionThesisPolicyResult).where(OptionThesisPolicyResult.thesis_id.in_(thesis_ids))
    return list(db.execute(stmt).scalars().all())


def upsert_option_thesis_policy_result(db: Session, thesis_id: int, policy: str, **fields) -> OptionThesisPolicyResult:
    row = db.get(OptionThesisPolicyResult, (thesis_id, policy))
    if row is None:
        row = OptionThesisPolicyResult(thesis_id=thesis_id, policy=policy, **fields)
        db.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row
