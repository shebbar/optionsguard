"""Holding-period scenario grid. Per DESIGN.md section 7.6: most trades are
exited within days, not held to expiry -- show what short holding periods
look like. Pure function, no I/O; IV is held constant across the grid."""
from __future__ import annotations

from datetime import date

from optionguard.pricing.bs import bs_price
from optionguard.pricing.calendar import trading_days_between, year_fraction

DAYS_OPTIONS = (1, 3, 5)
MOVE_OPTIONS = (-0.02, -0.01, 0.0, 0.01, 0.02)
EARNINGS_NOTE = "IV drop after results not modelled"


def compute_scenarios(
    *,
    spot: float,
    strike: float,
    opt_type: str,
    r: float,
    sigma: float,
    lot_size: int,
    entry_price: float,
    bid: float | None,
    ask: float | None,
    trading_days_to_expiry: int,
    trading_days_per_year: int,
    today: date,
    earnings_dates: list[date] | None = None,
    holidays: set[date] | None = None,
) -> list[dict]:
    half_spread = (ask - bid) / 2 if bid is not None and ask is not None else 0.0
    holidays = holidays or set()
    earnings_dates = earnings_dates or []

    scenarios: list[dict] = []
    for d in DAYS_OPTIONS:
        remaining_days = trading_days_to_expiry - d
        if remaining_days <= 0:
            continue
        T_d = year_fraction(remaining_days, trading_days_per_year)

        earnings_within = any(trading_days_between(today, ed, holidays) <= d for ed in earnings_dates)

        for m in MOVE_OPTIONS:
            est_value = bs_price(spot * (1 + m), strike, T_d, r, sigma, opt_type) - half_spread
            pnl_per_lot = (est_value - entry_price) * lot_size
            scenario = {
                "days": d,
                "move_pct": m,
                "est_value": est_value,
                "pnl_per_lot": pnl_per_lot,
            }
            if earnings_within:
                scenario["note"] = EARNINGS_NOTE
            scenarios.append(scenario)

    return scenarios
