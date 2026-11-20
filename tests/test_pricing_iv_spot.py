"""Section 7.9.4: implied_spot inverts bs_price for S (IV held constant) --
re-pricing the returned spot must reproduce the target price within
tolerance, for both CE and PE."""
from __future__ import annotations

import pytest

from optionguard.pricing.bs import bs_price
from optionguard.pricing.iv import implied_spot

K, T, R, SIGMA = 2500.0, 20 / 252, 0.065, 0.30


def test_call_spot_for_target_reprices_within_tolerance():
    target_price = 150.0
    spot = implied_spot(target_price, K, T, R, SIGMA, "CE")
    assert spot is not None
    repriced = bs_price(spot, K, T, R, SIGMA, "CE")
    assert repriced == pytest.approx(target_price, abs=0.01)


def test_put_spot_for_stop_reprices_within_tolerance():
    stop_price = 40.0
    spot = implied_spot(stop_price, K, T, R, SIGMA, "PE")
    assert spot is not None
    repriced = bs_price(spot, K, T, R, SIGMA, "PE")
    assert repriced == pytest.approx(stop_price, abs=0.01)


def test_higher_call_premium_implies_higher_spot():
    low = implied_spot(60.0, K, T, R, SIGMA, "CE")
    high = implied_spot(200.0, K, T, R, SIGMA, "CE")
    assert high > low


def test_returns_none_when_t_is_zero():
    assert implied_spot(60.0, K, 0.0, R, SIGMA, "CE") is None
