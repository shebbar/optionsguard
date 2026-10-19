"""Market data provider protocol. Mirrors DESIGN.md section 8."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable


class NotAvailable(Exception):
    """Raised when a provider cannot supply a given piece of data (e.g. manual
    mode has no history/chain). Callers degrade the dependent metrics to None
    and note the flag -- they must not treat this as a fatal error."""


@dataclass(slots=True)
class OptionQuote:
    symbol: str
    expiry: date
    strike: float
    opt_type: str  # "CE" | "PE"
    last_price: float
    bid: float | None
    ask: float | None
    volume: int
    oi: int
    lot_size: int


@dataclass(slots=True)
class DailyBar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@runtime_checkable
class MarketProvider(Protocol):
    def option_quote(self, symbol: str, expiry: date, strike: float, opt_type: str) -> OptionQuote: ...

    def spot(self, symbol: str) -> float: ...

    def history(self, symbol: str, days: int) -> list[DailyBar]: ...

    def chain(self, symbol: str, expiry: date, strikes: list[float]) -> list[OptionQuote]: ...

    def resolve_expiry(self, symbol: str, month: str) -> date: ...

    def lot_size(self, symbol: str, expiry: date) -> int: ...

    def list_strikes(self, symbol: str, expiry: date) -> list[float]: ...
