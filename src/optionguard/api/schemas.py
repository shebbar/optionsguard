from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel


class ManualPayload(BaseModel):
    spot: float
    premium: float
    lot_size: int
    expiry: date
    bid: float | None = None
    ask: float | None = None
    hv20: float | None = None
    volume: int | None = None
    oi: int | None = None


class CandidateIn(BaseModel):
    text: str
    thesis: str | None = None
    manual: ManualPayload | None = None


class ChecksRequest(BaseModel):
    candidates: list[CandidateIn]
    source: Literal["manual", "kite"] = "kite"


class TradeIn(BaseModel):
    check_id: int
    lots: int
    entry_premium: float
    stop_premium: float | None = None
    thesis_stop_level: float | None = None
    time_stop_date: date | None = None
    override_reason: str | None = None


class TightenIn(BaseModel):
    stop_premium: float | None = None
    thesis_stop_level: float | None = None
    time_stop_date: date | None = None


class LoosenIn(BaseModel):
    stop_premium: float | None = None
    thesis_stop_level: float | None = None
    time_stop_date: date | None = None
    reason: str


class ExitIn(BaseModel):
    exit_premium: float
    exit_reason: Literal["premium_stop", "thesis_stop", "time_stop", "target", "manual", "expiry"]
    charges: float | None = None


class EventIn(BaseModel):
    symbol: str
    event_type: Literal["earnings", "dividend"]
    event_date: date
    dividend: float | None = None


class SettingsIn(BaseModel):
    max_loss_per_trade: float | None = None
    monthly_loss_limit: float | None = None
    premium_stop_pct: float | None = None
    time_stop_days: int | None = None
    cooloff_days: int | None = None
    show_spreads: bool | None = None
