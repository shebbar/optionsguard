"""KiteProvider: implements MarketProvider against anything with the Kite
Connect client surface (instruments/quote/ltp/historical_data). Works
unchanged with MockKiteClient today or `kiteconnect.KiteConnect` once real
API credentials exist -- swap the client, not this file.

Per DESIGN.md section 5: resolve tradingsymbols by matching the instruments
dump on name/expiry/strike/instrument_type. Never construct symbols by
string formatting, never hardcode expiry weekdays.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

from optionguard.market.base import DailyBar, NotAvailable, OptionQuote
from optionguard.market.symbols import resolve_month_year

QUOTE_RATE_PER_SEC = 3.0


class _TokenBucket:
    """Simple token bucket so repeated quote()/ltp() calls stay within
    Kite's rate limit (DESIGN.md section 8: <= 3 requests/sec for quotes)."""

    def __init__(self, rate_per_sec: float) -> None:
        self.rate = rate_per_sec
        self.capacity = rate_per_sec
        self.tokens = rate_per_sec
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens < 1:
                time.sleep((1 - self.tokens) / self.rate)
                self.tokens = 0
            else:
                self.tokens -= 1


class KiteProvider:
    def __init__(self, client: Any) -> None:
        self.client = client
        self._nfo_cache: tuple[date, list[dict]] | None = None
        self._nse_cache: tuple[date, list[dict]] | None = None
        self._quote_bucket = _TokenBucket(QUOTE_RATE_PER_SEC)

    # ---- instruments (cached once per day) ---------------------------------

    def _nfo_instruments(self) -> list[dict]:
        today = date.today()
        if self._nfo_cache is None or self._nfo_cache[0] != today:
            self._nfo_cache = (today, self.client.instruments("NFO"))
        return self._nfo_cache[1]

    def _nse_instruments(self) -> list[dict]:
        today = date.today()
        if self._nse_cache is None or self._nse_cache[0] != today:
            self._nse_cache = (today, self.client.instruments("NSE"))
        return self._nse_cache[1]

    def _find_option(self, symbol: str, expiry: date, strike: float, opt_type: str) -> dict:
        for rec in self._nfo_instruments():
            if (
                rec["name"] == symbol.upper()
                and rec["expiry"] == expiry
                and float(rec["strike"]) == float(strike)
                and rec["instrument_type"] == opt_type.upper()
            ):
                return rec
        raise NotAvailable(
            f"No listed contract for {symbol} {expiry} {strike} {opt_type} in the instruments dump"
        )

    def _find_equity(self, symbol: str) -> dict:
        for rec in self._nse_instruments():
            if rec["tradingsymbol"] == symbol.upper():
                return rec
        raise NotAvailable(f"{symbol} not found in the NSE instruments dump")

    # ---- MarketProvider ----------------------------------------------------

    def resolve_expiry(self, symbol: str, month: str) -> date:
        year, month_num = resolve_month_year(month, date.today())
        candidates = sorted(
            {
                rec["expiry"]
                for rec in self._nfo_instruments()
                if rec["name"] == symbol.upper()
                and rec["expiry"].year == year
                and rec["expiry"].month == month_num
            }
        )
        if not candidates:
            raise NotAvailable(f"No listed expiry for {symbol} in {month} {year}")
        return candidates[0]

    def lot_size(self, symbol: str, expiry: date) -> int:
        for rec in self._nfo_instruments():
            if rec["name"] == symbol.upper() and rec["expiry"] == expiry:
                return int(rec["lot_size"])
        raise NotAvailable(f"No lot size found for {symbol} {expiry}")

    def list_strikes(self, symbol: str, expiry: date) -> list[float]:
        strikes = sorted(
            {
                float(rec["strike"])
                for rec in self._nfo_instruments()
                if rec["name"] == symbol.upper() and rec["expiry"] == expiry
            }
        )
        if not strikes:
            raise NotAvailable(f"No listed strikes for {symbol} {expiry}")
        return strikes

    def has_gtt(self, symbol: str, expiry: date, strike: float, opt_type: str) -> bool:
        """True if an active GTT order in Kite is keyed to this exact
        contract. Not part of the MarketProvider protocol -- manual mode has
        no GTT concept at all, so callers only use this for kite/mock trades.
        """
        rec = self._find_option(symbol, expiry, strike, opt_type)
        gtts = self.client.get_gtts()
        return any(
            g.get("status") == "active" and g.get("condition", {}).get("tradingsymbol") == rec["tradingsymbol"]
            for g in gtts
        )

    def spot(self, symbol: str) -> float:
        rec = self._find_equity(symbol)
        key = f"{rec['exchange']}:{rec['tradingsymbol']}"
        self._quote_bucket.acquire()
        quote = self.client.ltp([key])[key]
        return float(quote["last_price"])

    def option_quote(self, symbol: str, expiry: date, strike: float, opt_type: str) -> OptionQuote:
        rec = self._find_option(symbol, expiry, strike, opt_type)
        key = f"{rec['exchange']}:{rec['tradingsymbol']}"
        self._quote_bucket.acquire()
        q = self.client.quote([key])[key]
        depth = q.get("depth", {})
        best_bid = float(depth["buy"][0]["price"]) if depth.get("buy") else None
        best_ask = float(depth["sell"][0]["price"]) if depth.get("sell") else None
        return OptionQuote(
            symbol=symbol.upper(),
            expiry=expiry,
            strike=strike,
            opt_type=opt_type.upper(),
            last_price=float(q["last_price"]),
            bid=best_bid,
            ask=best_ask,
            volume=int(q.get("volume", 0)),
            oi=int(q.get("oi", 0)),
            lot_size=int(rec["lot_size"]),
        )

    def chain(self, symbol: str, expiry: date, strikes: list[float]) -> list[OptionQuote]:
        recs = [
            rec
            for rec in self._nfo_instruments()
            if rec["name"] == symbol.upper() and rec["expiry"] == expiry and float(rec["strike"]) in {float(s) for s in strikes}
        ]
        if not recs:
            raise NotAvailable(f"No contracts for {symbol} {expiry} at the requested strikes")
        keys = [f"{rec['exchange']}:{rec['tradingsymbol']}" for rec in recs]
        self._quote_bucket.acquire()
        quotes = self.client.quote(keys)
        out = []
        for rec in recs:
            key = f"{rec['exchange']}:{rec['tradingsymbol']}"
            q = quotes.get(key)
            if q is None:
                continue
            depth = q.get("depth", {})
            best_bid = float(depth["buy"][0]["price"]) if depth.get("buy") else None
            best_ask = float(depth["sell"][0]["price"]) if depth.get("sell") else None
            out.append(
                OptionQuote(
                    symbol=symbol.upper(),
                    expiry=expiry,
                    strike=float(rec["strike"]),
                    opt_type=rec["instrument_type"],
                    last_price=float(q["last_price"]),
                    bid=best_bid,
                    ask=best_ask,
                    volume=int(q.get("volume", 0)),
                    oi=int(q.get("oi", 0)),
                    lot_size=int(rec["lot_size"]),
                )
            )
        return out

    def history(self, symbol: str, days: int) -> list[DailyBar]:
        rec = self._find_equity(symbol)
        to_date = date.today()
        from_date = to_date - timedelta(days=int(days * 1.6) + 10)  # pad for weekends/holidays
        try:
            bars = self.client.historical_data(rec["instrument_token"], from_date, to_date, "day")
        except Exception as exc:  # the real client raises various kiteconnect exceptions
            raise NotAvailable(f"historical data unavailable for {symbol}: {exc}") from exc
        out = [
            DailyBar(
                day=datetime.fromisoformat(b["date"]).date() if isinstance(b["date"], str) else b["date"],
                open=float(b["open"]),
                high=float(b["high"]),
                low=float(b["low"]),
                close=float(b["close"]),
                volume=int(b["volume"]),
            )
            for b in bars
        ]
        return out[-days:]
