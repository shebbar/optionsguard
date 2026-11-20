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


@dataclass(slots=True)
class _Index:
    tradingsymbol: str
    base_price: float
    annual_vol: float


# NIFTY 50 and the sector indices from config.toml [sectors], for section 7.7's
# thesis journal (sector/market benchmark comparisons). Values are illustrative,
# not real market levels. Real Kite lists these in the NSE instruments dump
# tagged segment="INDICES" -- matched here on tradingsymbol, same as section 5
# requires for option contracts (never construct symbols by string formatting).
INDICES: dict[str, _Index] = {
    i.tradingsymbol: i
    for i in [
        _Index("NIFTY 50", 24000.0, 0.14),
        _Index("NIFTY IT", 42000.0, 0.20),
        _Index("NIFTY BANK", 52000.0, 0.16),
        _Index("NIFTY PSU BANK", 7000.0, 0.28),
        _Index("NIFTY FIN SERVICE", 23000.0, 0.18),
        _Index("NIFTY AUTO", 24000.0, 0.20),
        _Index("NIFTY INFRA", 9000.0, 0.18),
        _Index("NIFTY ENERGY", 40000.0, 0.20),
        _Index("NIFTY PHARMA", 22000.0, 0.16),
        _Index("NIFTY FMCG", 56000.0, 0.14),
        _Index("NIFTY METAL", 9500.0, 0.26),
        _Index("NIFTY REALTY", 1000.0, 0.30),
        _Index("NIFTY MEDIA", 1700.0, 0.30),
    ]
}


def _base_and_vol(key: str) -> tuple[float, float]:
    if key in UNDERLYINGS:
        u = UNDERLYINGS[key]
        return u.base_price, u.annual_vol
    if key in INDICES:
        idx = INDICES[key]
        return idx.base_price, idx.annual_vol
    raise KeyError(key)


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


def _last_trading_day(d: date) -> date:
    """Real exchanges don't trade on weekends, so a price/IV query made on
    one should resolve to Friday's close, not a fictional Saturday/Sunday
    value -- otherwise spot() and history() would disagree on exactly
    those two days of the week, and so would anything computed from them."""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


_IV_MULT_LO, _IV_MULT_HI = 0.7, 1.6
_IV_MULT_DAILY_STD = 0.01


def _iv_multiplier_path(symbol: str, end: date) -> list[float]:
    """A smoothly evolving IV-multiplier process from EPOCH to `end`, so
    iv/hv isn't always exactly 1.0 -- but evolves gradually day to day
    instead of independently re-randomizing within [0.85, 1.35] every single
    day, which produced option "daily" bars that swung 30-40% with no
    connection to the underlying's actual move (section 7.7's option
    historical_data needs a believable day-to-day series, not just a
    believable single snapshot)."""
    rng = random.Random(_seed("ivpath", symbol))
    n_days = (end - EPOCH).days + 1
    mult = [1.1]
    for _ in range(max(0, n_days - 1)):
        nxt = mult[-1] + rng.gauss(0, _IV_MULT_DAILY_STD)
        mult.append(min(max(nxt, _IV_MULT_LO), _IV_MULT_HI))
    return mult


def _iv_regime(symbol: str, annual_vol: float, as_of: date) -> float:
    """Deterministic given (symbol, date)."""
    return annual_vol * _iv_multiplier_path(symbol, as_of)[-1]


def _pseudo_int(*parts: str, lo: int, hi: int) -> int:
    rng = random.Random(_seed(*parts))
    return rng.randint(lo, hi)


_MINUTES_PER_SESSION = 375  # 09:15 to 15:29 IST inclusive


def _option_minute_bars_for_day(
    tradingsymbol: str, d: date, day_open: float, day_high: float, day_low: float, day_close: float,
) -> list[tuple[datetime, dict]]:
    """375 one-minute bars for one trading day, a seeded bridge walk from
    `day_open` to `day_close` -- a fresh Random per day, so a later day's
    walk doesn't shift if an earlier day's bars are ever regenerated.
    Clamped loosely to the day's established high/low; the exact close is
    forced on the last bar so it matches the mock's own daily bar exactly."""
    from optionguard.pricing.calendar import IST

    rng = random.Random(_seed("optmin", tradingsymbol, d.isoformat()))
    step_vol = max((day_high - day_low) * 0.12, 1e-4)
    lo_clamp, hi_clamp = day_low * 0.98, day_high * 1.02

    closes = [day_open]
    for i in range(1, _MINUTES_PER_SESSION + 1):
        frac = i / _MINUTES_PER_SESSION
        pull_target = day_open + (day_close - day_open) * frac
        noise = rng.gauss(0, step_vol) * (1 - 0.5 * frac)  # noise shrinks approaching the close
        nxt = min(max(pull_target + noise, lo_clamp), hi_clamp)
        closes.append(nxt)
    closes[-1] = day_close

    bars: list[tuple[datetime, dict]] = []
    session_start = datetime.combine(d, datetime.min.time(), tzinfo=IST).replace(hour=9, minute=15)
    for i in range(1, _MINUTES_PER_SESSION + 1):
        o, c = closes[i - 1], closes[i]
        ts = session_start + timedelta(minutes=i - 1)
        bars.append((ts, {
            "date": ts.isoformat(),
            "open": round(o, 2),
            "high": round(max(o, c) * 1.0005, 2),
            "low": round(min(o, c) * 0.9995, 2),
            "close": round(c, 2),
            "volume": _pseudo_int("optminvol", tradingsymbol, d.isoformat(), str(i), lo=0, hi=2_000),
            "oi": _pseudo_int("optoi", tradingsymbol, d.isoformat(), lo=0, hi=500_000),
        }))
    return bars


class MockKiteClient:
    """Offline stand-in for `kiteconnect.KiteConnect`."""

    def __init__(self, api_key: str = "mock", today: date | None = None) -> None:
        self.api_key = api_key
        self._today = today or date.today()
        self._instruments_nfo: list[dict] | None = None
        self._instruments_nse: list[dict] | None = None
        self._token_registry: dict[int, dict] = {}
        self._tradingsymbol_registry: dict[tuple[str, str], dict] = {}
        # Section 7.8: in-memory order/GTT stores. Not persisted -- fine for
        # a mock whose whole point is a deterministic, offline, single-
        # process stand-in; nothing here survives a process restart, same as
        # the instrument caches above.
        self._orders: dict[str, dict] = {}
        self._gtts: dict[int, dict] = {}
        self._next_order_seq = 1
        self._next_gtt_seq = 1

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
        for tsym in INDICES:
            rec = {
                "instrument_token": self._next_token(),
                "exchange_token": 0,
                "tradingsymbol": tsym,
                "name": tsym,
                "last_price": round(self._spot_internal(tsym), 2),
                "expiry": "",
                "strike": 0.0,
                "tick_size": 0.05,
                "lot_size": 1,
                # instrument_type="EQ" so historical_data()'s existing EQ-only
                # check below covers indices for free -- real Kite dumps are
                # less consistent about this field for indices; segment is
                # the field callers should actually match on (section 7.7.2).
                "instrument_type": "EQ",
                "segment": "INDICES",
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
        base_price, annual_vol = _base_and_vol(symbol)
        path = _price_path(symbol, base_price, annual_vol, _last_trading_day(self._today))
        return path[-1]

    def _option_premium(self, symbol: str, strike: float, expiry: date, opt_type: str, as_of: date | None = None) -> tuple[float, float]:
        """Returns (premium, iv_used). as_of defaults to self._today -- pass
        an earlier date to reconstruct a past day's premium for option
        historical_data() (section 7.7's thesis journal needs this)."""
        from optionguard.pricing.calendar import trading_days_to_expiry, year_fraction

        as_of = as_of or self._today
        u = UNDERLYINGS[symbol]
        trading_day = _last_trading_day(as_of)
        spot = _price_path(symbol, u.base_price, u.annual_vol, trading_day)[-1]
        iv = _iv_regime(symbol, u.annual_vol, trading_day)
        days = trading_days_to_expiry(as_of, expiry, holidays=set())
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
        """Before section 7.8, the mock never had real GTTs to return, so
        this was always empty. Now that place_gtt() actually tracks state,
        return it for real -- has_gtt() (market/kite.py) depends on this to
        warn when a position's stop isn't actually backed by one."""
        for gtt in self._gtts.values():
            self._maybe_trigger_gtt(gtt)
        return list(self._gtts.values())

    # ---- historical data ------------------------------------------------

    def historical_data(self, instrument_token: int, from_date, to_date, interval: str = "day") -> list[dict]:
        self._ensure_built()
        rec = self._token_registry.get(instrument_token)
        if rec is None:
            raise ValueError(f"unknown instrument_token {instrument_token}")

        if interval == "minute" and rec["instrument_type"] in ("CE", "PE"):
            return self._option_minute_data(rec, from_date, to_date)

        if isinstance(from_date, datetime):
            from_date = from_date.date()
        if isinstance(to_date, datetime):
            to_date = to_date.date()

        if rec["instrument_type"] in ("CE", "PE"):
            return self._option_historical_data(rec, from_date, to_date)

        if rec["instrument_type"] != "EQ":
            raise ValueError("historical_data is only available for NSE equity/index/option tokens in this mock")
        symbol = rec["tradingsymbol"]
        base_price, annual_vol = _base_and_vol(symbol)
        full_path = _price_path(symbol, base_price, annual_vol, max(to_date, self._today))
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

    def _option_historical_data(self, rec: dict, from_date: date, to_date: date) -> list[dict]:
        """Daily OHLC for an option contract, reconstructed by repricing it
        each day from that day's underlying path and IV regime -- not a
        second independent random walk. Section 7.7's thesis journal needs
        this for thesis_option_bars; real Kite's own equivalent doesn't
        cover expired contracts, which is why the sync step 1 (7.7.5) must
        run before expiry."""
        symbol, strike, expiry, opt_type = rec["name"], rec["strike"], rec["expiry"], rec["instrument_type"]
        out = []
        d = from_date
        while d <= to_date:
            if d.weekday() < 5 and d <= expiry:
                premium, _iv = self._option_premium(symbol, strike, expiry, opt_type, as_of=d)
                out.append({
                    "date": datetime.combine(d, datetime.min.time()).isoformat(),
                    "open": round(premium * 0.99, 2),
                    "high": round(premium * 1.02, 2),
                    "low": round(premium * 0.97, 2),
                    "close": round(premium, 2),
                    "volume": _pseudo_int("optvol", rec["tradingsymbol"], d.isoformat(), lo=0, hi=20_000),
                    "oi": _pseudo_int("optoi", rec["tradingsymbol"], d.isoformat(), lo=0, hi=500_000),
                })
            d += timedelta(days=1)
        return out

    def _option_minute_data(self, rec: dict, from_dt, to_dt) -> list[dict]:
        """Minute OHLC for an option contract, section 7.9.5 (paper theses
        need minute granularity to tell which of target/stop was touched
        first within a day -- daily bars can't). Each trading day's 375
        one-minute bars (09:15-15:29 IST) are a seeded bridge walk from that
        day's open to its close (same open/high/low/close this mock's daily
        option bars already use, so the two stay consistent with each
        other), not a second independent random process."""
        from optionguard.pricing.calendar import IST

        symbol, strike, expiry, opt_type = rec["name"], rec["strike"], rec["expiry"], rec["instrument_type"]
        tsym = rec["tradingsymbol"]

        from_dt = from_dt if isinstance(from_dt, datetime) else datetime.combine(from_dt, datetime.min.time())
        to_dt = to_dt if isinstance(to_dt, datetime) else datetime.combine(to_dt, datetime.max.time())
        if from_dt.tzinfo is None:
            from_dt = from_dt.replace(tzinfo=IST)
        if to_dt.tzinfo is None:
            to_dt = to_dt.replace(tzinfo=IST)

        bars: list[tuple[datetime, dict]] = []
        d = from_dt.date()
        while d <= to_dt.date():
            if d.weekday() < 5 and d <= expiry:
                premium, _iv = self._option_premium(symbol, strike, expiry, opt_type, as_of=d)
                day_open, day_close = premium * 0.99, premium
                day_high, day_low = premium * 1.02, premium * 0.97
                bars.extend(_option_minute_bars_for_day(tsym, d, day_open, day_high, day_low, day_close))
            d += timedelta(days=1)
        return [bar for ts, bar in bars if from_dt <= ts <= to_dt]

    # ---- orders (section 7.8) ----------------------------------------------
    # Deterministic fills: a BUY LIMIT at or above the current ask (or any
    # MARKET order) fills immediately at that touch price; a SELL LIMIT at
    # or below the current bid likewise. Otherwise the order just sits OPEN,
    # same as it would at a real exchange with no one to take the other side.

    def _touch_price(self, exchange: str, tradingsymbol: str, transaction_type: str) -> float:
        rec = self._tradingsymbol_registry.get((exchange, tradingsymbol))
        if rec is None:
            raise ValueError(f"unknown instrument {exchange}:{tradingsymbol}")
        q = self._quote_for_record(rec)
        side = q["depth"]["sell"] if transaction_type == "BUY" else q["depth"]["buy"]
        return side[0]["price"] if side else q["last_price"]

    def _next_order_id(self) -> str:
        oid = f"MOCK{self._today.strftime('%y%m%d')}{self._next_order_seq:06d}"
        self._next_order_seq += 1
        return oid

    def _evaluate_fill(self, order: dict) -> None:
        """Mutates `order` in place: OPEN unless the limit/market crosses
        the opposing touch price, exactly like place_order's own logic --
        reused by modify_order (a price change can turn an OPEN order into
        a fill) and by GTT triggering (7.8.5/7.8.6)."""
        touch = self._touch_price(order["exchange"], order["tradingsymbol"], order["transaction_type"])
        marketable = order["order_type"] == "MARKET" or (
            order["transaction_type"] == "BUY" and float(order["price"]) >= touch
        ) or (
            order["transaction_type"] == "SELL" and float(order["price"]) <= touch
        )
        if marketable:
            order["status"] = "COMPLETE"
            order["filled_quantity"] = order["quantity"]
            order["pending_quantity"] = 0
            order["average_price"] = round(touch, 2)
        else:
            order["status"] = "OPEN"
            order["filled_quantity"] = 0
            order["pending_quantity"] = order["quantity"]
            order["average_price"] = None

    def place_order(
        self, variety: str, exchange: str, tradingsymbol: str, transaction_type: str, quantity: int,
        product: str, order_type: str, price: float | None = None, validity: str = "DAY",
        tag: str | None = None, **kwargs,
    ) -> str:
        order = {
            "order_id": self._next_order_id(), "variety": variety, "exchange": exchange,
            "tradingsymbol": tradingsymbol, "transaction_type": transaction_type, "quantity": quantity,
            "product": product, "order_type": order_type, "price": price, "validity": validity, "tag": tag,
            "order_timestamp": datetime.combine(self._today, datetime.min.time()).isoformat(),
        }
        self._evaluate_fill(order)
        self._orders[order["order_id"]] = order
        return order["order_id"]

    def order_history(self, order_id: str) -> list[dict]:
        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(f"unknown order_id {order_id}")
        return [dict(order)]

    def modify_order(
        self, variety: str, order_id: str, price: float | None = None, quantity: int | None = None,
        order_type: str | None = None, **kwargs,
    ) -> str:
        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(f"unknown order_id {order_id}")
        if order["status"] != "OPEN":
            raise ValueError(f"order {order_id} is {order['status']}, not modifiable")
        if price is not None:
            order["price"] = price
        if quantity is not None:
            order["quantity"] = quantity
        if order_type is not None:
            order["order_type"] = order_type
        self._evaluate_fill(order)
        return order_id

    def cancel_order(self, variety: str, order_id: str, **kwargs) -> str:
        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(f"unknown order_id {order_id}")
        if order["status"] != "OPEN":
            raise ValueError(f"order {order_id} is {order['status']}, not cancellable")
        order["status"] = "CANCELLED"
        order["pending_quantity"] = 0
        return order_id

    # ---- GTT (section 7.8.5/7.8.6) -----------------------------------------

    def place_gtt(
        self, trigger_type: str, tradingsymbol: str, exchange: str, trigger_values: list[float],
        last_price: float, orders: list[dict],
    ) -> int:
        trigger_id = self._next_gtt_seq
        self._next_gtt_seq += 1
        self._gtts[trigger_id] = {
            "id": trigger_id, "type": trigger_type, "status": "active",
            "created_at": datetime.combine(self._today, datetime.min.time()).isoformat(),
            "condition": {
                "exchange": exchange, "tradingsymbol": tradingsymbol,
                "trigger_values": list(trigger_values), "last_price": last_price,
            },
            "orders": [dict(o) for o in orders],
        }
        return trigger_id

    def _maybe_trigger_gtt(self, gtt: dict) -> None:
        """Lazily evaluated on every get_gtt()/get_gtts() call, the same way
        spot() is lazily derived from the price path each call -- no
        background job needed to decide a GTT has fired."""
        if gtt["status"] != "active":
            return
        cond = gtt["condition"]
        current = self._touch_price(cond["exchange"], cond["tradingsymbol"], "SELL")  # LTP proxy
        trigger_values = cond["trigger_values"]
        crossed_idx = None
        if current <= min(trigger_values):
            crossed_idx = trigger_values.index(min(trigger_values))
        elif len(trigger_values) > 1 and current >= max(trigger_values):
            crossed_idx = trigger_values.index(max(trigger_values))
        if crossed_idx is None:
            return

        gtt["status"] = "triggered"
        leg = gtt["orders"][crossed_idx] if crossed_idx < len(gtt["orders"]) else gtt["orders"][0]
        resulting = {
            "order_id": self._next_order_id(), "variety": "regular", "exchange": cond["exchange"],
            "tradingsymbol": cond["tradingsymbol"], "transaction_type": leg["transaction_type"],
            "quantity": leg["quantity"], "product": leg["product"], "order_type": leg["order_type"],
            "price": leg["price"], "validity": "DAY",
            "order_timestamp": datetime.combine(self._today, datetime.min.time()).isoformat(),
        }
        self._evaluate_fill(resulting)
        self._orders[resulting["order_id"]] = resulting
        leg["result"] = {
            "order_id": resulting["order_id"], "status": resulting["status"],
            "average_price": resulting["average_price"], "filled_quantity": resulting["filled_quantity"],
        }

    def get_gtt(self, trigger_id: int) -> dict:
        gtt = self._gtts.get(trigger_id)
        if gtt is None:
            raise ValueError(f"unknown trigger_id {trigger_id}")
        self._maybe_trigger_gtt(gtt)
        return gtt

    def modify_gtt(
        self, trigger_id: int, trigger_type: str, tradingsymbol: str, exchange: str,
        trigger_values: list[float], last_price: float, orders: list[dict],
    ) -> int:
        gtt = self._gtts.get(trigger_id)
        if gtt is None:
            raise ValueError(f"unknown trigger_id {trigger_id}")
        if gtt["status"] != "active":
            raise ValueError(f"GTT {trigger_id} is {gtt['status']}, not modifiable")
        gtt["type"] = trigger_type
        gtt["condition"] = {
            "exchange": exchange, "tradingsymbol": tradingsymbol,
            "trigger_values": list(trigger_values), "last_price": last_price,
        }
        gtt["orders"] = [dict(o) for o in orders]
        return trigger_id

    def delete_gtt(self, trigger_id: int) -> int:
        gtt = self._gtts.get(trigger_id)
        if gtt is None:
            raise ValueError(f"unknown trigger_id {trigger_id}")
        gtt["status"] = "deleted"
        return trigger_id
