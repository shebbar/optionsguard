"""Section 7.9.4: the live-form derived numbers for an option thesis. Pure
functions, no I/O -- the caller supplies spot/iv/entry already fetched."""
from __future__ import annotations

from optionguard.pricing.iv import implied_spot
from optionguard.pricing.mc import simulate_target_stop_first


def reward_risk(entry: float, target: float, stop: float) -> float:
    return (target - entry) / (entry - stop)


def risk_per_lot(entry: float, stop: float, lot_size: int) -> float:
    return (entry - stop) * lot_size


def loss_vs_cap(entry: float, stop: float, lot_size: int, lots: int, max_loss_per_trade: float) -> float:
    return risk_per_lot(entry, stop, lot_size) * lots / max_loss_per_trade


def disaster_risk_per_lot(entry: float, disaster_stop_pct: float, lot_size: int) -> float:
    """7.9.7's form display: the ₹ risk per lot at the disaster stop, shown
    next to the normal stop's risk_per_lot. Sizing itself still uses the
    normal stop (section 7.3) -- this is information only."""
    return entry * disaster_stop_pct * lot_size


def spot_for_premium(
    price: float, strike: float, time_to_expiry_mid: float, r: float, iv: float, opt_type: str,
) -> float | None:
    """The underlying price at which the option's BS value equals `price`,
    at the midpoint of the thesis duration, IV held constant. None if no
    root is found (e.g. price unreachable given T/iv)."""
    return implied_spot(price, strike, time_to_expiry_mid, r, iv, opt_type)


def market_implied_odds(
    *, spot: float, strike: float, opt_type: str, r: float, iv: float, time_to_expiry_entry: float,
    duration_days: int, target_premium: float, stop_premium: float,
    mc_paths: int, mc_steps_per_day: int, mc_seed: int, trading_days_per_year: int = 252,
) -> dict:
    """mkt_prob_target_first/stop_first/mkt_prob_time_exit (7.9.4), via
    Monte Carlo. Deterministic for a fixed seed."""
    result = simulate_target_stop_first(
        spot=spot, strike=strike, opt_type=opt_type, r=r, iv=iv,
        time_to_expiry_entry=time_to_expiry_entry, duration_days=duration_days, steps_per_day=mc_steps_per_day,
        target_premium=target_premium, stop_premium=stop_premium, n_paths=mc_paths, seed=mc_seed,
        trading_days_per_year=trading_days_per_year,
    )
    return {
        "mkt_prob_target_first": result["target_first"],
        "mkt_prob_stop_first": result["stop_first"],
        "mkt_prob_time_exit": result["neither"],
    }


def compute_derived(
    *, entry: float, target: float, stop: float, lot_size: int, lots: int, max_loss_per_trade: float,
    spot: float, strike: float, opt_type: str, r: float, iv: float, duration_days: int,
    time_to_expiry_entry: float, mc_paths: int, mc_steps_per_day: int, mc_seed: int,
    trading_days_per_year: int = 252,
) -> dict:
    """Everything shown live on the new-thesis form (7.9.4), in one call."""
    time_to_expiry_mid = time_to_expiry_entry - (duration_days / 2) / trading_days_per_year
    odds = market_implied_odds(
        spot=spot, strike=strike, opt_type=opt_type, r=r, iv=iv, time_to_expiry_entry=time_to_expiry_entry,
        duration_days=duration_days, target_premium=target, stop_premium=stop,
        mc_paths=mc_paths, mc_steps_per_day=mc_steps_per_day, mc_seed=mc_seed,
        trading_days_per_year=trading_days_per_year,
    )
    return {
        "reward_risk": reward_risk(entry, target, stop),
        "risk_per_lot": risk_per_lot(entry, stop, lot_size),
        "loss_vs_cap": loss_vs_cap(entry, stop, lot_size, lots, max_loss_per_trade),
        "spot_for_target": spot_for_premium(target, strike, time_to_expiry_mid, r, iv, opt_type),
        "spot_for_stop": spot_for_premium(stop, strike, time_to_expiry_mid, r, iv, opt_type),
        **odds,
    }
