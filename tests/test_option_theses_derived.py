"""Section 7.9.4: the live-form derived numbers. Pure functions, no I/O."""
from __future__ import annotations

import pytest

from optionguard.option_theses import derived


def test_reward_risk_is_exact():
    assert derived.reward_risk(entry=50, target=80, stop=35) == pytest.approx((80 - 50) / (50 - 35))


def test_risk_per_lot_is_exact():
    assert derived.risk_per_lot(entry=50, stop=35, lot_size=175) == pytest.approx((50 - 35) * 175)


def test_loss_vs_cap_is_exact():
    assert derived.loss_vs_cap(entry=50, stop=35, lot_size=175, lots=2, max_loss_per_trade=5000) == pytest.approx(
        (50 - 35) * 175 * 2 / 5000
    )


def test_disaster_risk_per_lot_is_exact():
    assert derived.disaster_risk_per_lot(entry=50, disaster_stop_pct=0.6, lot_size=175) == pytest.approx(50 * 0.6 * 175)


def test_spot_for_premium_reprices_within_tolerance():
    from optionguard.pricing.bs import bs_price

    K, T, R, SIGMA = 2500.0, 20 / 252, 0.065, 0.30
    spot = derived.spot_for_premium(150.0, K, T, R, SIGMA, "CE")
    assert spot is not None
    assert bs_price(spot, K, T, R, SIGMA, "CE") == pytest.approx(150.0, abs=0.01)


def test_market_implied_odds_sum_to_one_and_are_deterministic():
    kwargs = dict(
        spot=2500.0, strike=2500.0, opt_type="CE", r=0.065, iv=0.30, time_to_expiry_entry=30 / 252,
        duration_days=5, mc_paths=5000, mc_steps_per_day=10, mc_seed=7,
    )
    a = derived.market_implied_odds(target_premium=120, stop_premium=60, **kwargs)
    b = derived.market_implied_odds(target_premium=120, stop_premium=60, **kwargs)
    assert a == b
    assert a["mkt_prob_target_first"] + a["mkt_prob_stop_first"] + a["mkt_prob_time_exit"] == pytest.approx(1.0)


def test_compute_derived_assembles_everything():
    out = derived.compute_derived(
        entry=50, target=80, stop=35, lot_size=175, lots=1, max_loss_per_trade=5000,
        spot=2500.0, strike=2500.0, opt_type="CE", r=0.065, iv=0.30, duration_days=5,
        time_to_expiry_entry=30 / 252, mc_paths=2000, mc_steps_per_day=10, mc_seed=7,
    )
    assert out["reward_risk"] == pytest.approx((80 - 50) / (50 - 35))
    assert out["risk_per_lot"] == pytest.approx((50 - 35) * 175)
    assert out["spot_for_target"] is not None
    assert out["spot_for_stop"] is not None
    assert out["mkt_prob_target_first"] + out["mkt_prob_stop_first"] + out["mkt_prob_time_exit"] == pytest.approx(1.0)
