from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


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


# ---- Section 7.8: order ticket ---------------------------------------------


class OrderReviewIn(BaseModel):
    kind: Literal["entry", "exit"]
    check_id: int | None = None    # required for kind="entry"
    trade_id: int | None = None    # required for kind="exit"
    lots: int | None = None        # entry: quantity in lots
    quantity: int | None = None    # exit: quantity in units (lots x lot_size); can be a partial exit
    price: float
    order_type: Literal["LIMIT", "MARKET"] = "LIMIT"
    stop_premium: float | None = None      # entry only; defaults from settings
    target_premium: float | None = None    # entry only; optional OCO target
    override_reason: str | None = None     # entry: red-verdict override; exit: exit_reason tag
    thesis_id: int | None = None           # entry only; section 7.9.10, "Save & buy" from an option thesis


class OrderConfirmIn(BaseModel):
    intent_id: int


class OrderModifyIn(BaseModel):
    price: float | None = None
    quantity: int | None = None


# ---- Section 7.9: option theses ---------------------------------------------


class OptionThesisPreviewIn(BaseModel):
    contract: str  # "SYMBOL MON STRIKE CE|PE", same syntax as checks (section 5)
    source: Literal["manual", "kite"] = "kite"
    manual: ManualPayload | None = None  # required when source="manual"
    target_premium: float
    stop_premium: float
    duration_days: int
    lots: int = 1


class OptionThesisIn(BaseModel):
    check_id: int  # the check just run, or one under 30 minutes old
    mode: Literal["paper", "real"] = "paper"
    why: str = Field(min_length=10, max_length=200)
    target_premium: float
    stop_premium: float
    duration_days: int
    lots: int = 1
    sector: str | None = None  # defaults from [symbol_sectors] if omitted
    override_reason: str | None = None  # required (>=15 chars) if the check's verdict is red
    notes: str | None = None


class OptionThesisNotesIn(BaseModel):
    notes: str


class OptionThesisExitIn(BaseModel):
    exit_time: str  # ISO timestamp -- the minute (or day) bar this claims to have exited in
    price: float
    reason: Literal["judgement", "news", "other"]
    text: str | None = None


class OptionThesisCancelIn(BaseModel):
    reason: str = Field(min_length=15)


class OptionThesisLinkTradeIn(BaseModel):
    trade_id: int
