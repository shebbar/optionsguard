"""Per DESIGN.md section 7.6: daily_move_needed and delta."""
from __future__ import annotations

from datetime import date

from optionguard.analysis.metrics import CandidateRequest, compute_metrics
from optionguard.config import get_config
from optionguard.market.manual import ManualInput, ManualProvider
from optionguard.pricing import bs
from optionguard.pricing.calendar import trading_days_to_expiry, year_fraction


def _manual_provider(**overrides):
    defaults = dict(
        symbol="TCS", expiry=date(2026, 11, 26), strike=2500, opt_type="CE",
        spot=2480, premium=35, lot_size=175,
    )
    defaults.update(overrides)
    return ManualProvider(ManualInput(**defaults))


def test_daily_move_needed_equals_theta_over_delta():
    cfg = get_config()
    today = date(2026, 10, 15)
    provider = _manual_provider()
    req = CandidateRequest(symbol="TCS", expiry=date(2026, 11, 26), strike=2500, opt_type="CE", data_source="manual")
    m = compute_metrics(provider, req, config=cfg, today=today)

    assert m.iv is not None, "IV must resolve for this to be a meaningful test"
    holidays = set(cfg.holidays.dates)
    days_to_expiry = trading_days_to_expiry(today, req.expiry, holidays)
    T = year_fraction(days_to_expiry, cfg.pricing.trading_days_per_year)
    g = bs.greeks(m.spot, req.strike, T, cfg.pricing.risk_free_rate, m.iv, req.opt_type, cfg.pricing.trading_days_per_year)

    assert m.delta == g["delta"]
    expected = abs(g["theta_per_day"]) / abs(g["delta"])
    assert m.daily_move_needed == expected
    assert m.daily_move_needed_pct == expected / m.spot


def test_daily_move_needed_none_when_delta_too_small():
    cfg = get_config()
    today = date(2026, 10, 15)
    # Deep OTM, days from expiry -- delta should be tiny.
    provider = _manual_provider(strike=4000, premium=0.5)
    req = CandidateRequest(symbol="TCS", expiry=date(2026, 11, 26), strike=4000, opt_type="CE", data_source="manual")
    m = compute_metrics(provider, req, config=cfg, today=today)

    if m.delta is not None and abs(m.delta) < 0.05:
        assert m.daily_move_needed is None
        assert m.daily_move_needed_pct is None
