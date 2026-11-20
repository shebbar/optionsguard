"""Section 7.9.5: resolving a paper option thesis's outcome from its
option's own OHLC bars (minute, or daily as a fallback). Pure functions, no
I/O -- the thin wrapper that fetches bars lives in option_theses/sync.py.

The option's own premium is checked against target/stop directly -- there's
no "direction" concept here (unlike section 7.7's stock-price theses):
target_premium > entry_premium > stop_premium always, by validation, so a
single bar can never satisfy both simultaneously."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class Bar:
    ts: datetime
    o: float
    h: float
    l: float
    c: float


def resolve_paper(entry: float, target: float, stop: float, bars: list[Bar], slippage_pct: float) -> dict:
    """Walks `bars` in time order (already trimmed to start at the first
    full bar after entry) and returns the first of target/stop/neither,
    applying slippage against the user on every exit -- the user is long
    the option and selling to exit, so slippage always means a lower exit
    price. `bars` may be minute or daily; the caller tags which via the
    'bars' field on the stored score, not this function."""
    if not bars:
        return {"outcome": "unresolvable", "exit_time": None, "exit_premium": None, "ambiguous_bar": False}

    for bar in bars:
        if bar.o <= stop:
            return {"outcome": "stop", "exit_time": bar.ts, "exit_premium": bar.o * (1 - slippage_pct), "ambiguous_bar": False}
        if bar.o >= target:
            return {"outcome": "target", "exit_time": bar.ts, "exit_premium": bar.o * (1 - slippage_pct), "ambiguous_bar": False}

        stop_hit = bar.l <= stop
        target_hit = bar.h >= target
        if stop_hit and target_hit:
            return {"outcome": "stop", "exit_time": bar.ts, "exit_premium": stop * (1 - slippage_pct), "ambiguous_bar": True}
        if stop_hit:
            return {"outcome": "stop", "exit_time": bar.ts, "exit_premium": stop * (1 - slippage_pct), "ambiguous_bar": False}
        if target_hit:
            return {"outcome": "target", "exit_time": bar.ts, "exit_premium": target * (1 - slippage_pct), "ambiguous_bar": False}

    last = bars[-1]
    return {
        "outcome": "time", "exit_time": last.ts, "exit_premium": last.c * (1 - slippage_pct), "ambiguous_bar": False,
    }


def find_bar_at(bars: list[Bar], ts: datetime) -> Bar | None:
    """The bar whose minute (or day) `ts` falls in, for manual-exit price
    validation. Minute bars are keyed by exact start timestamp; this just
    does a linear scan since the lists here are small (one duration's
    worth, not a full history)."""
    for bar in bars:
        if bar.ts == ts:
            return bar
    return None


def validate_manual_exit_price(price: float, bar: Bar) -> bool:
    """7.9.5: a manual exit's price must fall within the low-high of the
    bar at that exit time (minute bar, or that day's bar if minute data is
    missing)."""
    return bar.l <= price <= bar.h


def is_late_manual_exit(exit_time: datetime, entered_at: datetime, late_exit_flag_days: int) -> bool:
    """7.9.5: a manual exit entered more than `late_exit_flag_days` after
    its claimed exit time gets flagged `late_entry`."""
    return (entered_at - exit_time).days > late_exit_flag_days
