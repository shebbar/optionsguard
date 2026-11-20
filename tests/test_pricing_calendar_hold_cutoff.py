"""Section 7.9.7: trading_day_on_or_before -- the hold cutoff date for a
contract. "For a 29 Dec expiry with hold_cutoff_calendar_days = 7, the
cutoff is 22 Dec. If 22 Dec isn't a trading day, it's the trading day
before" (DESIGN_7_9_option_theses.md 7.9.7)."""
from __future__ import annotations

from datetime import date, timedelta

from optionguard.pricing.calendar import trading_day_on_or_before

HOLD_CUTOFF_CALENDAR_DAYS = 7


def _cutoff(expiry: date, holidays: set[date]) -> date:
    return trading_day_on_or_before(expiry - timedelta(days=HOLD_CUTOFF_CALENDAR_DAYS), holidays)


def test_29_dec_expiry_gives_22_dec_cutoff_when_22_dec_is_a_trading_day():
    expiry = date(2026, 12, 29)  # a Tuesday
    assert _cutoff(expiry, holidays=set()) == date(2026, 12, 22)  # the preceding Tuesday


def test_cutoff_snaps_back_when_the_raw_date_is_a_weekend():
    # 7 calendar days is a multiple of 7, so expiry - 7 days always falls on
    # the same weekday as expiry itself -- a Sunday expiry snaps back to Friday.
    expiry = date(2026, 12, 27)  # a Sunday
    raw = expiry - timedelta(days=HOLD_CUTOFF_CALENDAR_DAYS)
    assert raw.weekday() == 6  # Sunday
    assert _cutoff(expiry, holidays=set()) == date(2026, 12, 18)  # Friday


def test_cutoff_snaps_back_further_when_the_raw_date_is_a_holiday():
    expiry = date(2026, 12, 29)
    raw = expiry - timedelta(days=HOLD_CUTOFF_CALENDAR_DAYS)  # 2026-12-22, a Tuesday
    assert _cutoff(expiry, holidays={raw}) == date(2026, 12, 21)  # the Monday before


def test_cutoff_itself_is_always_a_trading_day():
    expiry = date(2026, 11, 26)  # a Thursday
    cutoff = _cutoff(expiry, holidays=set())
    assert cutoff.weekday() < 5
