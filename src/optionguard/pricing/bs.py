"""Black-Scholes pricing and greeks. Pure functions, no I/O.

European exercise, no dividends here (the escrowed-dividend adjustment to S
per DESIGN.md section 5 happens in the caller, which passes an adjusted S).
"""
from __future__ import annotations

import numpy as np
from scipy.stats import norm

CallPut = str  # "CE" | "PE"


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    if T <= 0 or sigma <= 0:
        raise ValueError("T and sigma must be positive")
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return d1, d2


def bs_price(S: float, K: float, T: float, r: float, sigma: float, opt_type: CallPut) -> float:
    if T <= 0:
        return max(0.0, (S - K) if opt_type == "CE" else (K - S))
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if opt_type == "CE":
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    elif opt_type == "PE":
        return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    raise ValueError(f"opt_type must be CE or PE, got {opt_type!r}")


def intrinsic_value(S: float, K: float, opt_type: CallPut) -> float:
    return max(0.0, (S - K) if opt_type == "CE" else (K - S))


def greeks(
    S: float, K: float, T: float, r: float, sigma: float, opt_type: CallPut,
    trading_days_per_year: int = 252,
) -> dict[str, float]:
    if T <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta_per_day": 0.0, "vega": 0.0}
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    pdf_d1 = norm.pdf(d1)
    gamma = pdf_d1 / (S * sigma * np.sqrt(T))
    vega = (S * pdf_d1 * np.sqrt(T)) / 100  # price change per 1 vol point (1%)

    if opt_type == "CE":
        delta = norm.cdf(d1)
        theta_annual = (
            -(S * pdf_d1 * sigma) / (2 * np.sqrt(T)) - r * K * np.exp(-r * T) * norm.cdf(d2)
        )
    elif opt_type == "PE":
        delta = norm.cdf(d1) - 1
        theta_annual = (
            -(S * pdf_d1 * sigma) / (2 * np.sqrt(T)) + r * K * np.exp(-r * T) * norm.cdf(-d2)
        )
    else:
        raise ValueError(f"opt_type must be CE or PE, got {opt_type!r}")

    return {
        "delta": float(delta),
        "gamma": float(gamma),
        "theta_per_day": float(theta_annual / trading_days_per_year),
        "vega": float(vega),
    }
