"""A fake Kite Connect client. Deliberately mirrors the method names and
response *shapes* of the real `kiteconnect.KiteConnect` client (instruments,
quote, ltp, historical_data, login_url, generate_session) so that
market/kite.py -- which only talks to this client surface -- can later point
at a real KiteConnect(api_key) instance with no rewrite.

Everything here is deterministic and offline: prices follow a seeded random
walk keyed by symbol, so repeated calls on the same day return the same
numbers, and calls on a later day return a slightly different, continuous
path. There is no real network I/O.
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from optionguard.pricing.bs import bs_price

EPOCH = date(2024, 1, 1)
TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE = 0.065


@dataclass(slots=True)
class _Underlying:
    symbol: str
    name: str
    base_price: float
    annual_vol: float
    strike_step: float
    lot_size: int


# A small fixed universe of liquid NSE large-caps. Values are illustrative,
# not real market data.
UNDERLYINGS: dict[str, _Underlying] = {
    u.symbol: u
    for u in [
        _Underlying("RELIANCE", "RELIANCE INDUSTRIES", 2900.0, 0.22, 20, 250),
        _Underlying("TCS", "TATA CONSULTANCY SERVICES", 4100.0, 0.20, 50, 175),
        _Underlying("INFY", "INFOSYS", 1850.0, 0.26, 20, 400),
        _Underlying("HDFCBANK", "HDFC BANK", 1700.0, 0.20, 20, 550),
        _Underlying("ICICIBANK", "ICICI BANK", 1250.0, 0.22, 10, 700),
        _Underlying("SBIN", "STATE BANK OF INDIA", 820.0, 0.28, 10, 1500),
        _Underlying("TATAMOTORS", "TATA MOTORS", 780.0, 0.35, 10, 2850),
        _Underlying("AXISBANK", "AXIS BANK", 1150.0, 0.24, 10, 625),
        _Underlying("BHARTIARTL", "BHARTI AIRTEL", 1600.0, 0.21, 20, 475),
        _Underlying("MARUTI", "MARUTI SUZUKI", 11500.0, 0.19, 100, 50),
    ]
}
LOT_SIZES: dict[str, int] = {u.symbol: u.lot_size for u in UNDERLYINGS.values()}
_NUM_EXPIRY_MONTHS = 4
_STRIKES_EACH_SIDE = 10


def _seed(*parts: str) -> int:
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(h, 16) % (2**32)


def _last_thursday(year: int, month: int) -> date:
    if month == 12:
        first_of_next = date(year + 1, 1, 1)
    else:
        first_of_next = date(year, month + 1, 1)
    d = first_of_next - timedelta(days=1)
    while d.weekday() != 3:  # Thursday
        d -= timedelta(days=1)
    return d


def monthly_expiries(today: date, count: int = _NUM_EXPIRY_MONTHS) -> list[date]:
    """Next `count` monthly (last-Thursday) expiries from `today`, inclusive
    of the current month if not already expired."""
    out: list[date] = []
    year, month = today.year, today.month
    while len(out) < count:
        exp = _last_thursday(year, month)
        if exp >= today:
            out.append(exp)
        month += 1
        if month > 12:
            month = 1
            year += 1
    return out


def _price_path(symbol: str, base_price: float, annual_vol: float, end: date) -> list[float]:
    """Deterministic daily-close random walk from EPOCH to `end` inclusive."""
    rng = random.Random(_seed("path", symbol))
    n_days = (end - EPOCH).days + 1
    dt = 1 / TRADING_DAYS_PER_YEAR
    prices = [base_price]
    for _ in range(max(0, n_days - 1)):
        z = rng.gauss(0, 1)
        prev = prices[-1]
        prices.append(prev * math.exp((-0.5 * annual_vol**2) * dt + annual_vol * math.sqrt(dt) * z))
    return prices


def _iv_regime(symbol: str, annual_vol: float, as_of: date) -> float:
    """A per-day IV multiplier on top of the realised-vol path, so iv/hv
    isn't always exactly 1.0. Deterministic given (symbol, date)."""
    rng = random.Random(_seed("ivregime", symbol, as_of.isoformat()))
    return annual_vol * rng.uniform(0.85, 1.35)


def _pseudo_int(*parts: str, lo: int, hi: int) -> int:
    rng = random.Random(_seed(*parts))
    return rng.randint(lo, hi)


class MockKiteClient:
    """Offline stand-in for `kiteconnect.KiteConnect`."""

    def __init__(self, api_key: str = "mock", today: date | None = None) -> None:
        self.api_key = api_key
        self._today = today or date.today()
        self._instruments_nfo: list[dict] | None = None
        self._instruments_nse: list[dict] | None = None
        self._token_registry: dict[int, dict] = {}
        self._tradingsymbol_registry: dict[tuple[str, str], dict] = {}

    # ---- auth -----------------------------------------------------------

    def login_url(self) -> str:
        return f"https://kite.mock/connect/login?api_key={self.api_key}&v=3"

    def generate_session(self, request_token: str, api_secret: str) -> dict:
        return {
            "access_token": f"mock-access-token-{self._today.isoformat()}",
            "public_token": "mock-public-token",
            "user_id": "MOCK001",
            "user_name": "Mock User",
            "login_time": datetime.combine(self._today, datetime.min.time()).isoformat(),
        }

    # ---- instruments ------------------------------------------------------

    def instruments(self, exchange: str) -> list[dict]:
        if exchange == "NFO":
            if self._instruments_nfo is None:
                self._instruments_nfo = self._build_nfo_instruments()
            return self._instruments_nfo
        if exchange == "NSE":
            if self._instruments_nse is None:
                self._instruments_nse = self._build_nse_instruments()
            return self._instruments_nse
        return []

    def _next_token(self) -> int:
        return 100000 + len(self._token_registry)

    def _register(self, record: dict) -> None:
        token = record["instrument_token"]
        self._token_registry[token] = record
        self._tradingsymbol_registry[(record["exchange"], record["tradingsymbol"])] = record

    def _build_nse_instruments(self) -> list[dict]:
        out = []
        for sym, u in UNDERLYINGS.items():
            rec = {
                "instrument_token": self._next_token(),
                "exchange_token": 0,
                "tradingsymbol": sym,
                "name": u.name,
                "last_price": round(self._spot_internal(sym), 2),
                "expiry": "",
                "strike": 0.0,
                "tick_size": 0.05,
                "lot_size": 1,
                "instrument_type": "EQ",
                "segment": "NSE",
                "exchange": "NSE",
            }
            self._register(rec)
            out.append(rec)
        return out

    def _build_nfo_instruments(self) -> list[dict]:
        out = []
        expiries = monthly_expiries(self._today)
        for sym, u in UNDERLYINGS.items():
            # Centre the strike grid on today's (random-walked) spot, not the
            # fixed seed price -- otherwise the grid goes stale as the mock
            # price path drifts away from base_price over simulated time.
            live_spot = self._spot_internal(sym)
            base = round(live_spot / u.strike_step) * u.strike_step
            strikes = [
                base + i * u.strike_step
                for i in range(-_STRIKES_EACH_SIDE, _STRIKES_EACH_SIDE + 1)
            ]
            for expiry in expiries:
                for strike in strikes:
                    for opt_type in ("CE", "PE"):
                        tradingsymbol = (
                            f"{sym}{expiry.strftime('%y%b').upper()}{int(strike)}{opt_type}"
                        )
                        rec = {
                            "instrument_token": self._next_token(),
                            "exchange_token": 0,
                            "tradingsymbol": tradingsymbol,
                            # Real Kite NFO dumps put the short underlying
                            # code here (e.g. "TCS"), not the full company
                            # name -- that's only true for the NSE/EQ dump.
                            "name": sym,
                            "last_price": 0.0,
                            "expiry": expiry,
                            "strike": float(strike),
                            "tick_size": 0.05,
                            "lot_size": LOT_SIZES[sym],
                            "instrument_type": opt_type,
                            "segment": "NFO-OPT",
                            "exchange": "NFO",
                        }
                        self._register(rec)
                        out.append(rec)
        return out

    def _ensure_built(self) -> None:
        self.instruments("NFO")
        self.instruments("NSE")

    # ---- prices -----------------------------------------------------------

    def _spot_internal(self, symbol: str) -> float:
        u = UNDERLYINGS[symbol]
        path = _price_path(symbol, u.base_price, u.annual_vol, self._today)
        return path[-1]

    def _option_premium(self, symbol: str, strike: float, expiry: date, opt_type: str) -> tuple[float, float]:
        """Returns (premium, iv_used)."""
        from optionguard.pricing.calendar import trading_days_to_expiry, year_fraction

        u = UNDERLYINGS[symbol]
        spot = self._spot_internal(symbol)
        iv = _iv_regime(symbol, u.annual_vol, self._today)
        days = trading_days_to_expiry(self._today, expiry, holidays=set())
        T = year_fraction(max(days, 1))
        premium = bs_price(spot, strike, T, RISK_FREE_RATE, iv, opt_type)
        return max(premium, 0.05), iv

    # ---- quote / ltp ------------------------------------------------------

    def quote(self, instruments: list[str]) -> dict[str, dict]:
        self._ensure_built()
        out = {}
        for key in instruments:
            exch, tsym = key.split(":", 1)
            rec = self._tradingsymbol_registry.get((exch, tsym))
            if rec is None:
                continue
            out[key] = self._quote_for_record(rec)
        return out

    def _quote_for_record(self, rec: dict) -> dict:
        now = datetime.combine(self._today, datetime.min.time())
        if rec["instrument_type"] == "EQ":
            last_price = round(self._spot_internal(rec["tradingsymbol"]), 2)
            volume = _pseudo_int("eqvol", rec["tradingsymbol"], self._today.isoformat(), lo=200_000, hi=5_000_000)
            oi = 0
            bid, ask = round(last_price - 0.05, 2), round(last_price + 0.05, 2)
        else:
            symbol_key = rec["name"]
            premium, _iv = self._option_premium(symbol_key, rec["strike"], rec["expiry"], rec["instrument_type"])
            spread = max(premium * 0.03, 0.05)
            bid, ask = round(premium - spread / 2, 2), round(premium + spread / 2, 2)
            last_price = round(premium, 2)
            volume = _pseudo_int("optvol", rec["tradingsymbol"], self._today.isoformat(), lo=0, hi=20_000)
            oi = _pseudo_int("optoi", rec["tradingsymbol"], self._today.isoformat(), lo=0, hi=500_000)

        depth_buy = [
            {"price": round(bid - i * 0.05, 2), "quantity": _pseudo_int("depthb", rec["tradingsymbol"], str(i), lo=1, hi=500) * rec["lot_size"], "orders": 1}
            for i in range(5)
        ]
        depth_sell = [
            {"price": round(ask + i * 0.05, 2), "quantity": _pseudo_int("depths", rec["tradingsymbol"], str(i), lo=1, hi=500) * rec["lot_size"], "orders": 1}
            for i in range(5)
        ]
        return {
            "instrument_token": rec["instrument_token"],
            "timestamp": now.isoformat(),
            "last_trade_time": now.isoformat(),
            "last_price": last_price,
            "last_quantity": rec["lot_size"],
            "buy_quantity": sum(d["quantity"] for d in depth_buy),
            "sell_quantity": sum(d["quantity"] for d in depth_sell),
            "volume": volume,
            "average_price": last_price,
            "oi": oi,
            "oi_day_high": int(oi * 1.05),
            "oi_day_low": int(oi * 0.95),
            "net_change": 0.0,
            "lower_circuit_limit": round(last_price * 0.8, 2),
            "upper_circuit_limit": round(last_price * 1.2, 2),
            "ohlc": {
                "open": round(last_price * 0.99, 2),
                "high": round(last_price * 1.02, 2),
                "low": round(last_price * 0.98, 2),
                "close": round(last_price * 0.995, 2),
            },
            "depth": {"buy": depth_buy, "sell": depth_sell},
        }

    def ltp(self, instruments: list[str]) -> dict[str, dict]:
        full = self.quote(instruments)
        return {k: {"instrument_token": v["instrument_token"], "last_price": v["last_price"]} for k, v in full.items()}

    # ---- GTT ---------------------------------------------------------------

    def get_gtts(self) -> list[dict]:
        """The mock never has real orders placed in a real Kite account, so
        this is always empty -- consistent with there being no actual GTT
        protecting a mock position."""
        return []

    # ---- historical data ------------------------------------------------

    def historical_data(self, instrument_token: int, from_date, to_date, interval: str = "day") -> list[dict]:
        self._ensure_built()
        rec = self._token_registry.get(instrument_token)
        if rec is None or rec["instrument_type"] != "EQ":
            raise ValueError("historical_data is only available for NSE equity tokens in this mock")
        if isinstance(from_date, datetime):
            from_date = from_date.date()
        if isinstance(to_date, datetime):
            to_date = to_date.date()
        symbol = rec["tradingsymbol"]
        u = UNDERLYINGS[symbol]
        full_path = _price_path(symbol, u.base_price, u.annual_vol, max(to_date, self._today))
        out = []
        d = EPOCH
        for i, close in enumerate(full_path):
            if from_date <= d <= to_date and d.weekday() < 5:
                prev = full_path[i - 1] if i > 0 else close
                vol = _pseudo_int("histvol", symbol, d.isoformat(), lo=100_000, hi=3_000_000)
                out.append({
                    "date": datetime.combine(d, datetime.min.time()).isoformat(),
                    "open": round(prev, 2),
                    "high": round(max(prev, close) * 1.004, 2),
                    "low": round(min(prev, close) * 0.996, 2),
                    "close": round(close, 2),
                    "volume": vol,
                    "oi": 0,
                })
            d += timedelta(days=1)
        return out
