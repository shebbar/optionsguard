"""SQLAlchemy 2.x models -- Postgres. Mirrors DESIGN.md section 10, minus the
`alerts` table (Telegram/monitor-loop alerting is descoped from v1)."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
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
