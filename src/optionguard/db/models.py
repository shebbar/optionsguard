"""SQLAlchemy 2.x models -- Postgres. Mirrors DESIGN.md section 10, minus the
`alerts` table (Telegram/monitor-loop alerting is descoped from v1)."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Settings(Base):
    """Append-only. The active row is the latest one with effective_from <= now."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_loss_per_trade: Mapped[float] = mapped_column(Numeric, nullable=False)
    monthly_loss_limit: Mapped[float] = mapped_column(Numeric, nullable=False)
    premium_stop_pct: Mapped[float] = mapped_column(Numeric, nullable=False)
    time_stop_days: Mapped[int] = mapped_column(nullable=False)
    cooloff_days: Mapped[int] = mapped_column(nullable=False)
    show_spreads: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CorporateEvent(Base):
    __tablename__ = "corporate_events"
    __table_args__ = (
        CheckConstraint("event_type IN ('earnings','dividend')", name="ck_event_type"),
        UniqueConstraint("symbol", "event_type", "event_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    dividend: Mapped[float | None] = mapped_column(Numeric, nullable=True)


class Check(Base):
    __tablename__ = "checks"
    __table_args__ = (
        CheckConstraint("opt_type IN ('CE','PE')", name="ck_opt_type"),
        CheckConstraint("data_source IN ('manual','mock','kite')", name="ck_data_source"),
        CheckConstraint("verdict IN ('red','amber','green')", name="ck_verdict"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    batch_id: Mapped[str] = mapped_column(nullable=False)
    symbol: Mapped[str] = mapped_column(nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    strike: Mapped[float] = mapped_column(Numeric, nullable=False)
    opt_type: Mapped[str] = mapped_column(nullable=False)
    data_source: Mapped[str] = mapped_column(nullable=False)

    spot: Mapped[float] = mapped_column(Numeric, nullable=False)
    premium: Mapped[float] = mapped_column(Numeric, nullable=False)
    bid: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    ask: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    lot_size: Mapped[int] = mapped_column(nullable=False)

    iv: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    hv20: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    hv_percentile: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    breakeven: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    breakeven_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    expected_move_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    prob_profit: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    prob_zero: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    prob_thesis: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    theta_per_day_lot: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    delta: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    daily_move_needed: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    stretch_5d_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    stretch_20dma_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    spread_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)

    thesis_target: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    thesis_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    verdict: Mapped[str] = mapped_column(nullable=False)
    flags: Mapped[list] = mapped_column(JSONB, nullable=False)
    max_lots: Mapped[int] = mapped_column(nullable=False)
    alternatives: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    oi_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)

    expiry_settle: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        CheckConstraint("status IN ('open','closed')", name="ck_trade_status"),
        CheckConstraint(
            "exit_reason IN ('premium_stop','thesis_stop','time_stop','target','manual','expiry')",
            name="ck_exit_reason",
        ),
        CheckConstraint(
            "gtt_status IS NULL OR gtt_status IN ('active','missing','triggered','deleted')",
            name="ck_trade_gtt_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(ForeignKey("checks.id"), nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lots: Mapped[int] = mapped_column(nullable=False)
    lot_size: Mapped[int] = mapped_column(nullable=False)
    entry_premium: Mapped[float] = mapped_column(Numeric, nullable=False)
    stop_premium: Mapped[float] = mapped_column(Numeric, nullable=False)
    thesis_stop_level: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    time_stop_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, default="open")
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_premium: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(nullable=True)
    charges: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    # Section 7.8, added. order_intents.trade_id -> trades.id the other way
    # (an exit intent's trade is known upfront); use_alter breaks the cycle
    # so create_all()/Alembic can create both tables without one needing to
    # exist before the other.
    entry_intent_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_intents.id", use_alter=True, name="fk_trades_entry_intent"), nullable=True
    )
    exit_intent_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_intents.id", use_alter=True, name="fk_trades_exit_intent"), nullable=True
    )
    gtt_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gtt_status: Mapped[str | None] = mapped_column(nullable=True)


class StopChange(Base):
    __tablename__ = "stop_changes"
    __table_args__ = (
        CheckConstraint("direction IN ('tighten','loosen')", name="ck_direction"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    field: Mapped[str] = mapped_column(nullable=False)
    old_value: Mapped[str | None] = mapped_column(nullable=True)
    new_value: Mapped[str | None] = mapped_column(nullable=True)
    direction: Mapped[str] = mapped_column(nullable=False)


class Override(Base):
    __tablename__ = "overrides"
    __table_args__ = (
        CheckConstraint("length(reason) >= 15", name="ck_reason_len"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int | None] = mapped_column(ForeignKey("checks.id"), nullable=True)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rule: Mapped[str] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(nullable=False)


class KiteSession(Base):
    __tablename__ = "kite_session"
    __table_args__ = (CheckConstraint("id = 1", name="ck_singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    access_token: Mapped[str | None] = mapped_column(nullable=True)
    valid_for: Mapped[date | None] = mapped_column(Date, nullable=True)


# ---- Section 7.7: thesis journal ------------------------------------------


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint("trigger IN ('login','button','cli')", name="ck_sync_trigger"),
        CheckConstraint("status IN ('running','ok','partial','failed')", name="ck_sync_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trigger: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False)
    counts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    errors: Mapped[list | None] = mapped_column(JSONB, nullable=True)


class OrderIntent(Base):
    """Section 7.8.7: a reviewed-then-confirmed order. Idempotency: Confirm
    is only accepted for an intent in 'reviewed' status, via a conditional
    UPDATE ... WHERE status = 'reviewed' (repo.try_transition_order_intent) --
    never reachable from /api/v1 or MCP (7.8.1's non-goal)."""

    __tablename__ = "order_intents"
    __table_args__ = (
        CheckConstraint("kind IN ('entry','exit')", name="ck_intent_kind"),
        CheckConstraint("order_type IN ('LIMIT','MARKET')", name="ck_intent_order_type"),
        CheckConstraint(
            "status IN ('reviewed','placed','filled','partial','rejected','cancelled','expired')",
            name="ck_intent_status",
        ),
        CheckConstraint(
            "(kind = 'entry' AND check_id IS NOT NULL) OR (kind = 'exit' AND trade_id IS NOT NULL)",
            name="ck_intent_kind_matches_ref",
        ),
        Index(
            "ux_order_intents_live_contract", "tradingsymbol", unique=True,
            postgresql_where=text("status IN ('reviewed','placed')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(nullable=False)
    check_id: Mapped[int | None] = mapped_column(ForeignKey("checks.id"), nullable=True)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), nullable=True)
    tradingsymbol: Mapped[str] = mapped_column(nullable=False)
    quantity: Mapped[int] = mapped_column(nullable=False)
    order_type: Mapped[str] = mapped_column(nullable=False)
    price: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    stop_premium: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    target_premium: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    override_reason: Mapped[str | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(nullable=False)
    order_id: Mapped[str | None] = mapped_column(nullable=True)
    filled_qty: Mapped[int | None] = mapped_column(nullable=True)
    average_price: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    kite_message: Mapped[str | None] = mapped_column(nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Section 7.9, added. One-way reference -- option_theses already exists
    # by the time this column is added, so no use_alter cycle here (unlike
    # trades.entry_intent_id/exit_intent_id above).
    option_thesis_id: Mapped[int | None] = mapped_column(
        ForeignKey("option_theses.id", name="fk_order_intents_option_thesis"), nullable=True
    )


class OptionThesis(Base):
    """Section 7.9.9: a planned trade on one option contract (entry, target
    premium, stop premium, duration). Immutable after creation except
    notes/status freely, and outcome/exit_time/exit_premium/
    manual_exit_reason/manual_exit_entered_at/trade_id/cancelled_at/
    cancel_reason each settable exactly once (null -> a value), and
    entry_premium exactly once for a 'real' thesis not yet linked to a
    trade (when it's updated to the fill price) -- enforced in db/repo.py
    and by a Postgres trigger (second line of defence)."""

    __tablename__ = "option_theses"
    __table_args__ = (
        CheckConstraint("opt_type IN ('CE','PE')", name="ck_opt_thesis_opt_type"),
        CheckConstraint("mode IN ('paper','real')", name="ck_opt_thesis_mode"),
        CheckConstraint("data_source IN ('manual','mock','kite')", name="ck_opt_thesis_data_source"),
        CheckConstraint("verdict IN ('red','amber','green')", name="ck_opt_thesis_verdict"),
        CheckConstraint("status IN ('open','closed','scored','cancelled')", name="ck_opt_thesis_status"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('target','stop','time','manual','unresolvable')",
            name="ck_opt_thesis_outcome",
        ),
        CheckConstraint("length(why) BETWEEN 10 AND 200", name="ck_opt_thesis_why_len"),
        CheckConstraint(
            "cancel_reason IS NULL OR length(cancel_reason) >= 15", name="ck_opt_thesis_cancel_reason_len"
        ),
        CheckConstraint(
            "stop_premium < entry_premium AND entry_premium < target_premium", name="ck_opt_thesis_premium_order"
        ),
        CheckConstraint("end_date <= expiry", name="ck_opt_thesis_end_before_expiry"),
        CheckConstraint("lots >= 1", name="ck_opt_thesis_lots"),
        CheckConstraint(
            "mode = 'paper' OR trade_id IS NOT NULL OR status IN ('open','cancelled')",
            name="ck_opt_thesis_real_needs_trade",
        ),
        Index(
            "ux_option_theses_open_contract", "symbol", "expiry", "strike", "opt_type",
            unique=True, postgresql_where=text("status = 'open'"),
        ),
        Index("ix_option_theses_status_end", "status", "end_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbol: Mapped[str] = mapped_column(nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    strike: Mapped[float] = mapped_column(Numeric, nullable=False)
    opt_type: Mapped[str] = mapped_column(nullable=False)
    opt_instrument_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mode: Mapped[str] = mapped_column(nullable=False)
    data_source: Mapped[str] = mapped_column(nullable=False)
    sector: Mapped[str] = mapped_column(nullable=False)
    sector_index: Mapped[str | None] = mapped_column(nullable=True)
    why: Mapped[str] = mapped_column(nullable=False)
    check_id: Mapped[int] = mapped_column(ForeignKey("checks.id"), nullable=False)
    verdict: Mapped[str] = mapped_column(nullable=False)
    check_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    override_id: Mapped[int | None] = mapped_column(ForeignKey("overrides.id"), nullable=True)
    lot_size: Mapped[int] = mapped_column(nullable=False)
    lots: Mapped[int] = mapped_column(nullable=False)
    entry_premium: Mapped[float] = mapped_column(Numeric, nullable=False)
    target_premium: Mapped[float] = mapped_column(Numeric, nullable=False)
    stop_premium: Mapped[float] = mapped_column(Numeric, nullable=False)
    duration_days: Mapped[int] = mapped_column(nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    reward_risk: Mapped[float] = mapped_column(Numeric, nullable=False)
    spot_for_target: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    spot_for_stop: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    mkt_prob_target_first: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    mkt_prob_stop_first: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    mkt_prob_time_exit: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    spot_entry: Mapped[float] = mapped_column(Numeric, nullable=False)
    sector_index_entry: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    market_index_entry: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    entry_flags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), nullable=True)
    notes: Mapped[str | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(nullable=False, default="open")
    outcome: Mapped[str | None] = mapped_column(nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_premium: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    manual_exit_reason: Mapped[str | None] = mapped_column(nullable=True)
    manual_exit_entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(nullable=True)


class OptionMinuteBar(Base):
    """Section 7.9.5/7.9.9. Shared across every option thesis on the same
    contract, at minute granularity -- needed to tell which of target/stop
    was touched first within a day."""

    __tablename__ = "option_minute_bars"

    instrument_token: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    o: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    h: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    l: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    c: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OptionThesisScore(Base):
    """Section 7.9.6/7.9.9. Rescoring replaces this row (score_version
    bumps); it never edits the linked OptionThesis row."""

    __tablename__ = "option_thesis_scores"
    __table_args__ = (
        CheckConstraint("bars IN ('minute','daily_fallback','none','fill')", name="ck_opt_score_bars"),
    )

    thesis_id: Mapped[int] = mapped_column(ForeignKey("option_theses.id"), primary_key=True)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    score_version: Mapped[int] = mapped_column(nullable=False)
    days_held: Mapped[int] = mapped_column(nullable=False)
    ret_pct: Mapped[float] = mapped_column(Numeric, nullable=False)
    pnl: Mapped[float] = mapped_column(Numeric, nullable=False)
    charges: Mapped[float] = mapped_column(Numeric, nullable=False)
    r_multiple: Mapped[float] = mapped_column(Numeric, nullable=False)
    mfe_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    mfe_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mae_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    mae_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_after_stop: Mapped[bool | None] = mapped_column(nullable=True)
    planned_outcome: Mapped[str | None] = mapped_column(nullable=True)
    planned_r: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    spot_ret_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    sector_ret_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    market_ret_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    bars: Mapped[str] = mapped_column(nullable=False)
    flags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    path: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class OptionThesisPolicyResult(Base):
    """Section 7.9.7/7.9.9: one row per (thesis, exit policy), replaced
    wholesale on every rescore -- not immutable, unlike OptionThesis."""

    __tablename__ = "option_thesis_policy_results"
    __table_args__ = (
        CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('target','stop','disaster','time_checkpoint','time','hold_end','unresolvable')",
            name="ck_opt_policy_outcome",
        ),
    )

    thesis_id: Mapped[int] = mapped_column(ForeignKey("option_theses.id"), primary_key=True)
    policy: Mapped[str] = mapped_column(primary_key=True)
    policy_params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    outcome: Mapped[str | None] = mapped_column(nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_premium: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    days_held: Mapped[int | None] = mapped_column(nullable=True)
    pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    pending: Mapped[bool] = mapped_column(nullable=False, default=False)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
