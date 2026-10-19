"""Implied volatility via Brent's method. Pure function, no I/O."""
from __future__ import annotations

from scipy.optimize import brentq

from optionguard.pricing.bs import bs_price, intrinsic_value

SIGMA_LO = 0.01
SIGMA_HI = 3.0


def implied_vol(
    price: float, S: float, K: float, T: float, r: float, opt_type: str
) -> float | None:
    """Returns None if `price` is below intrinsic value or no root is found
    in [0.01, 3.0] -- the caller should flag this as 'price inconsistent /
    stale' rather than raise."""
    if T <= 0:
        return None
    if price < intrinsic_value(S, K, opt_type) - 1e-9:
        return None

    def f(sigma: float) -> float:
        return bs_price(S, K, T, r, sigma, opt_type) - price

    lo, hi = f(SIGMA_LO), f(SIGMA_HI)
    if lo > 0 or hi < 0:
        return None
    try:
        return float(brentq(f, SIGMA_LO, SIGMA_HI, xtol=1e-6))
    except ValueError:
        return None
