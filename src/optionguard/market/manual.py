"""Manual data source: every value comes from the request payload. Per
DESIGN.md section 8, `history` and `chain` raise NotAvailable so the caller
degrades the dependent metrics to None with a flag, and `resolve_expiry` is
unused -- manual candidates carry an explicit expiry in the payload instead
of a bare month name."""
from __future__ import annotations

from datetime import date

from optionguard.market.base import DailyBar, NotAvailable, OptionQuote


class ManualInput:
    """One manual candidate's raw fields, as posted to /checks."""

    def __init__(
        self,
        *,
        symbol: str,
        expiry: date,
        strike: float,
        opt_type: str,
        spot: float,
        premium: float,
        lot_size: int,
        bid: float | None = None,
        ask: float | None = None,
        hv20: float | None = None,
        volume: int | None = None,
        oi: int | None = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.expiry = expiry
        self.strike = strike
        self.opt_type = opt_type.upper()
        self.spot = spot
        self.premium = premium
        self.lot_size = lot_size
        self.bid = bid
        self.ask = ask
        self.hv20 = hv20
        self.volume = volume
        self.oi = oi


class ManualProvider:
    """Backed by a single ManualInput -- one instance per candidate."""

    def __init__(self, data: ManualInput) -> None:
        self.data = data

    def option_quote(self, symbol: str, expiry: date, strike: float, opt_type: str) -> OptionQuote:
        d = self.data
        return OptionQuote(
            symbol=d.symbol,
            expiry=d.expiry,
            strike=d.strike,
            opt_type=d.opt_type,
            last_price=d.premium,
            bid=d.bid,
            ask=d.ask,
            volume=d.volume or 0,
            oi=d.oi or 0,
            lot_size=d.lot_size,
        )

    def spot(self, symbol: str) -> float:
        return self.data.spot

    def history(self, symbol: str, days: int) -> list[DailyBar]:
        raise NotAvailable("manual mode has no price history")

    def chain(self, symbol: str, expiry: date, strikes: list[float]) -> list[OptionQuote]:
        raise NotAvailable("manual mode has no option chain -- connect Kite for alternatives")

    def resolve_expiry(self, symbol: str, month: str) -> date:
        raise NotAvailable("manual mode requires an explicit expiry in the payload")

    def lot_size(self, symbol: str, expiry: date) -> int:
        return self.data.lot_size

    def list_strikes(self, symbol: str, expiry: date) -> list[float]:
        raise NotAvailable("manual mode has no option chain -- connect Kite for OI context")
