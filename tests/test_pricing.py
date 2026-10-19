from __future__ import annotations

import numpy as np
import pytest

from optionguard.pricing.bs import bs_price, greeks, intrinsic_value
from optionguard.pricing.iv import implied_vol
from optionguard.pricing.probability import prob_above, prob_profit, prob_zero


# Hull's textbook example: S=42, K=40, r=0.10, sigma=0.20, T=0.5 -> call ~= 4.76
def test_bs_price_matches_textbook_example():
    price = bs_price(42, 40, 0.5, 0.10, 0.20, "CE")
    assert price == pytest.approx(4.76, abs=0.01)


@pytest.mark.parametrize("S,K,T,r,sigma", [(100, 100, 0.5, 0.05, 0.2), (50, 60, 1.0, 0.03, 0.4), (200, 180, 0.1, 0.0, 0.15)])
def test_put_call_parity(S, K, T, r, sigma):
    call = bs_price(S, K, T, r, sigma, "CE")
    put = bs_price(S, K, T, r, sigma, "PE")
    assert call - put == pytest.approx(S - K * np.exp(-r * T), abs=1e-6)


@pytest.mark.parametrize("opt_type,moneyness", [("CE", 0.8), ("CE", 1.0), ("CE", 1.2), ("PE", 0.8), ("PE", 1.0), ("PE", 1.1)])
def test_iv_round_trip(opt_type, moneyness):
    # Deep-ITM puts with a large r*T are deliberately excluded here: the BS
    # price itself can fall below intrinsic for a European put in that
    # regime (no early exercise), which is correctly a None case -- see
    # test_implied_vol_none_below_intrinsic.
    S, T, r, sigma = 100.0, 0.4, 0.05, 0.25
    K = S * moneyness
    price = bs_price(S, K, T, r, sigma, opt_type)
    iv = implied_vol(price, S, K, T, r, opt_type)
    assert iv == pytest.approx(sigma, abs=1e-4)


def test_implied_vol_none_below_intrinsic():
    S, K, T, r, opt_type = 120.0, 100.0, 0.5, 0.05, "CE"
    below_intrinsic_price = intrinsic_value(S, K, opt_type) - 1.0
    assert implied_vol(below_intrinsic_price, S, K, T, r, opt_type) is None


def test_probabilities_within_bounds():
    for K in (80, 100, 120):
        p = prob_above(100, K, 0.5, 0.05, 0.3)
        assert 0.0 <= p <= 1.0


def test_prob_above_monotone_in_strike():
    probs = [prob_above(100, K, 0.5, 0.05, 0.3) for K in (60, 80, 100, 120, 140)]
    assert probs == sorted(probs, reverse=True)


def test_prob_profit_and_zero_bounds():
    for opt_type in ("CE", "PE"):
        pp = prob_profit(100, 105, 0.3, 0.05, 0.25, opt_type)
        pz = prob_zero(100, 100, 0.3, 0.05, 0.25, opt_type)
        assert 0.0 <= pp <= 1.0
        assert 0.0 <= pz <= 1.0


def test_greeks_call_put_delta_relationship():
    g_call = greeks(100, 100, 0.5, 0.05, 0.25, "CE")
    g_put = greeks(100, 100, 0.5, 0.05, 0.25, "PE")
    assert g_call["delta"] - g_put["delta"] == pytest.approx(1.0, abs=1e-6)
    assert g_call["gamma"] == pytest.approx(g_put["gamma"], abs=1e-9)
