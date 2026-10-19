"""Per DESIGN.md section 7.6: holding-period scenario grid."""
from __future__ import annotations

from datetime import date

import pytest

from optionguard.analysis.scenarios import compute_scenarios

BASE = dict(
    spot=100.0, strike=100.0, r=0.065, sigma=0.25, lot_size=100,
    trading_days_to_expiry=30, trading_days_per_year=252, today=date(2026, 10, 15),
)


def test_zero_move_after_one_day_decays_below_entry():
    entry = 5.5
    scenarios = compute_scenarios(opt_type="CE", entry_price=entry, bid=5.3, ask=5.5, **BASE)
    zero_1d = next(s for s in scenarios if s["days"] == 1 and s["move_pct"] == 0.0)
    assert zero_1d["est_value"] < entry


def test_pe_pnl_increases_as_move_goes_more_negative():
    scenarios = compute_scenarios(opt_type="PE", entry_price=5.5, bid=5.3, ask=5.5, **BASE)
    one_day = sorted((s for s in scenarios if s["days"] == 1), key=lambda s: s["move_pct"])
    pnls = [s["pnl_per_lot"] for s in one_day]
    assert pnls == sorted(pnls, reverse=True)  # most negative move -> highest P&L


def test_ce_pnl_increases_as_move_goes_more_positive():
    scenarios = compute_scenarios(opt_type="CE", entry_price=5.5, bid=5.3, ask=5.5, **BASE)
    one_day = sorted((s for s in scenarios if s["days"] == 1), key=lambda s: s["move_pct"])
    pnls = [s["pnl_per_lot"] for s in one_day]
    assert pnls == sorted(pnls)  # most positive move -> highest P&L


def test_skips_days_beyond_expiry():
    scenarios = compute_scenarios(
        opt_type="CE", entry_price=5.5, bid=None, ask=None,
        spot=100.0, strike=100.0, r=0.065, sigma=0.25, lot_size=100,
        trading_days_to_expiry=3, trading_days_per_year=252, today=date(2026, 10, 15),
    )
    assert sorted({s["days"] for s in scenarios}) == [1]  # 3d and 5d would exceed expiry


def test_earnings_within_window_gets_noted():
    # 1 trading day out -- falls within every d in {1, 3, 5}, so every scenario is noted.
    scenarios = compute_scenarios(
        opt_type="CE", entry_price=5.5, bid=None, ask=None,
        earnings_dates=[date(2026, 10, 16)], **BASE,
    )
    assert all(s.get("note") == "IV drop after results not modelled" for s in scenarios)


def test_earnings_outside_window_is_not_noted():
    # Today is 2026-10-15 (Thursday); 2026-10-26 is well beyond 5 trading days out.
    scenarios = compute_scenarios(
        opt_type="CE", entry_price=5.5, bid=None, ask=None,
        earnings_dates=[date(2026, 10, 26)], **BASE,
    )
    assert not any("note" in s for s in scenarios)


@pytest.mark.parametrize("opt_type", ["CE", "PE"])
def test_scenario_grid_has_15_cells_when_nothing_skipped(opt_type):
    scenarios = compute_scenarios(opt_type=opt_type, entry_price=5.5, bid=5.3, ask=5.5, **BASE)
    assert len(scenarios) == 15
