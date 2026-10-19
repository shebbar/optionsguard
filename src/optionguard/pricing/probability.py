"""Risk-neutral lognormal probabilities implied by market prices -- not
forecasts. Pure functions, no I/O."""
from __future__ import annotations

import numpy as np
from scipy.stats import norm


def prob_above(S: float, X: float, T: float, r: float, sigma: float) -> float:
    """P(S_T > X) under risk-neutral GBM, i.e. N(d2) with K = X."""
    if T <= 0:
        return 1.0 if S > X else 0.0
    d2 = (np.log(S / X) + (r - 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    return float(norm.cdf(d2))


def prob_profit(S: float, breakeven: float, T: float, r: float, sigma: float, opt_type: str) -> float:
    p_above = prob_above(S, breakeven, T, r, sigma)
    return p_above if opt_type == "CE" else 1 - p_above


def prob_zero(S: float, K: float, T: float, r: float, sigma: float, opt_type: str) -> float:
    p_above_k = prob_above(S, K, T, r, sigma)
    return (1 - p_above_k) if opt_type == "CE" else p_above_k


def prob_touch(S: float, X: float, T: float, r: float, sigma: float) -> float:
    """Approx probability of touching X by T: min(1, 2 * P(finish beyond X)).
    Used only for the thesis target, per DESIGN.md section 6."""
    p_finish_beyond = prob_above(S, X, T, r, sigma) if X >= S else 1 - prob_above(S, X, T, r, sigma)
    return min(1.0, 2 * p_finish_beyond)


def expected_move_pct(sigma: float, T: float) -> float:
    return sigma * np.sqrt(T)
