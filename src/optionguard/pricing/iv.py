"""Inverting Black-Scholes via Brent's method -- for IV (given a price,
solve for sigma) and for spot (given a price, solve for S). Pure functions,
no I/O."""
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


def implied_spot(
    price: float, K: float, T: float, r: float, sigma: float, opt_type: str,
) -> float | None:
    """Section 7.9.4: the underlying price at which bs_price(S, K, T, r,
    sigma, opt_type) equals `price`, with IV (sigma) held constant -- used
    for spot_for_target/spot_for_stop. bs_price is monotonic in S (strictly
    increasing for a call, strictly decreasing for a put), so a wide bracket
    around K is used rather than around S, since the answer may be far from
    today's spot. Returns None if T <= 0 or no root is found in the bracket."""
    if T <= 0:
        return None

    def f(S: float) -> float:
        return bs_price(S, K, T, r, sigma, opt_type) - price

    lo, hi = K * 1e-4, K * 50
    f_lo, f_hi = f(lo), f(hi)
    if opt_type == "CE":
        if f_lo > 0 or f_hi < 0:
            return None
    elif opt_type == "PE":
        if f_lo < 0 or f_hi > 0:
            return None
    else:
        raise ValueError(f"opt_type must be CE or PE, got {opt_type!r}")
    try:
        return float(brentq(f, lo, hi, xtol=1e-4))
    except ValueError:
        return None
