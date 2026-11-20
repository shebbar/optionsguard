"""Section 7.9.4: Monte Carlo odds of reaching the target premium before the
stop (or neither), for an option thesis. Pure function, no I/O. Vectorized
over paths and time steps with numpy -- the scalar bs_price() in bs.py isn't
used here since calling it once per (path, step) in a Python loop would be
far too slow at mc_paths * duration_days * mc_steps_per_day evaluations."""
from __future__ import annotations

import numpy as np
from scipy.stats import norm


def _bs_price_matrix(S: np.ndarray, K: float, T: np.ndarray, r: float, sigma: float, opt_type: str) -> np.ndarray:
    """Black-Scholes price over an (n_paths, n_steps) grid of spots S and a
    (n_steps,) vector of remaining-time-to-expiry T, broadcasting K/r/sigma
    as scalars. Same formula as bs.bs_price, vectorized."""
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if opt_type == "CE":
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    if opt_type == "PE":
        return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    raise ValueError(f"opt_type must be CE or PE, got {opt_type!r}")


def simulate_target_stop_first(
    *,
    spot: float,
    strike: float,
    opt_type: str,
    r: float,
    iv: float,
    time_to_expiry_entry: float,
    duration_days: int,
    steps_per_day: int,
    target_premium: float,
    stop_premium: float,
    n_paths: int,
    seed: int,
    trading_days_per_year: int = 252,
) -> dict:
    """Simulates the underlying under risk-neutral GBM at `iv`, repricing
    the option (IV held constant) at every step, and counts which of
    target/stop is reached first over `duration_days`. Returns
    {"target_first", "stop_first", "neither"}, summing to 1.0 -- deterministic
    for a fixed seed. target_premium/stop_premium must straddle the entry
    premium strictly (enforced by the caller at thesis creation), so a path
    can never satisfy both in the same step."""
    n_steps = duration_days * steps_per_day
    duration_years = duration_days / trading_days_per_year
    dt = duration_years / n_steps
    if time_to_expiry_entry - duration_years <= 0:
        raise ValueError("duration runs past (or to) expiry -- caller must validate end_date against expiry first")

    rng = np.random.default_rng(seed)
    z = rng.standard_normal((n_paths, n_steps))
    increments = (r - 0.5 * iv**2) * dt + iv * np.sqrt(dt) * z
    log_s = np.log(spot) + np.cumsum(increments, axis=1)
    s_t = np.exp(log_s)

    elapsed = np.arange(1, n_steps + 1) * dt  # (n_steps,)
    t_remaining = time_to_expiry_entry - elapsed  # strictly > 0 given the guard above

    opt_prices = _bs_price_matrix(s_t, strike, t_remaining, r, iv, opt_type)

    target_hit = opt_prices >= target_premium
    stop_hit = opt_prices <= stop_premium

    first_target = np.where(target_hit.any(axis=1), target_hit.argmax(axis=1), n_steps)
    first_stop = np.where(stop_hit.any(axis=1), stop_hit.argmax(axis=1), n_steps)

    target_first = first_target < first_stop
    stop_first = first_stop < first_target
    neither = (first_target == n_steps) & (first_stop == n_steps)

    return {
        "target_first": float(target_first.mean()),
        "stop_first": float(stop_first.mean()),
        "neither": float(neither.mean()),
    }
