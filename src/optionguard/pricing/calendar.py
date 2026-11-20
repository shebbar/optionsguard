"""Trading-day calendar. NSE: Mon-Fri, minus config.toml [holidays]. No I/O --
the holiday list is passed in by the caller (read from config)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)


def now_ist() -> datetime:
    return datetime.now(IST)


def is_trading_day(d: date, holidays: set[date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def next_trading_day(d: date, holidays: set[date]) -> date:
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt, holidays):
        nxt += timedelta(days=1)
    return nxt


def trading_days_between(start: date, end: date, holidays: set[date]) -> int:
    """Number of trading days strictly after `start` up to and including `end`.
    0 if end <= start."""
    if end <= start:
        return 0
    count = 0
    d = start + timedelta(days=1)
    while d <= end:
        if is_trading_day(d, holidays):
            count += 1
        d += timedelta(days=1)
    return count


def trading_days_to_expiry(today: date, expiry: date, holidays: set[date]) -> int:
    return trading_days_between(today, expiry, holidays)


def trading_day_before(d: date, n: int, holidays: set[date]) -> date:
    """n trading days before `d`."""
    count = 0
    cur = d
    while count < n:
        cur -= timedelta(days=1)
        if is_trading_day(cur, holidays):
            count += 1
    return cur


def trading_day_after(d: date, n: int, holidays: set[date]) -> date:
    """n trading days after `d`. Used by section 7.7 to turn a thesis's
    horizon_days into horizon_end."""
    count = 0
    cur = d
    while count < n:
        cur += timedelta(days=1)
        if is_trading_day(cur, holidays):
            count += 1
    return cur


def trading_day_on_or_before(d: date, holidays: set[date]) -> date:
    """`d` itself if it's a trading day, else the nearest trading day before
    it. Section 7.9.7's hold cutoff date is fixed this way: "the last
    trading day on or before expiry - hold_cutoff_calendar_days"."""
    cur = d
    while not is_trading_day(cur, holidays):
        cur -= timedelta(days=1)
    return cur


def year_fraction(trading_days: int, trading_days_per_year: int = 252) -> float:
    """T = trading_days_to_expiry / 252, per DESIGN.md section 5."""
    return trading_days / trading_days_per_year


def is_market_hours(dt: datetime | None = None) -> bool:
    dt = (dt or now_ist()).astimezone(IST)
    if dt.weekday() >= 5:
        return False
    open_t = dt.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    close_t = dt.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    return open_t <= dt <= close_t
