"""Section 7.9.6: per-thesis scoring. Pure function, no I/O -- the thin
wrapper that fetches bars and index levels lives in option_theses/sync.py.

Known simplification against 7.9.10's chart spec: `path` here only carries
the option's own downsampled closes, not the underlying's -- there's no
underlying minute-bar table in this build (only the option's), so a second-
axis spot overlay on the chart isn't available yet."""
from __future__ import annotations

from datetime import date, datetime

from optionguard.option_theses.resolve import Bar
from optionguard.pricing.calendar import trading_days_between

SCORE_VERSION = 1
PATH_DOWNSAMPLE_MINUTES = 15


def _target_after_stop(outcome: str, exit_time: datetime | None, target_premium: float, bars: list[Bar]) -> bool:
    """7.9.6: the stop fired, then the target was reached before end_date."""
    if outcome != "stop" or exit_time is None:
        return False
    return any(b.ts > exit_time and b.h >= target_premium for b in bars)


def _mfe_mae(entry_premium: float, bars: list[Bar]) -> dict:
    if not bars:
        return {"mfe_pct": None, "mfe_time": None, "mae_pct": None, "mae_time": None}
    best = max(bars, key=lambda b: b.h)
    worst = min(bars, key=lambda b: b.l)
    return {
        "mfe_pct": (best.h - entry_premium) / entry_premium, "mfe_time": best.ts,
        "mae_pct": (worst.l - entry_premium) / entry_premium, "mae_time": worst.ts,
    }


def _index_ret_pct(entry_level: float | None, exit_level: float | None) -> float | None:
    if entry_level is None or exit_level is None or entry_level == 0:
        return None
    return (exit_level - entry_level) / entry_level


def _downsampled_path(bars: list[Bar], every_minutes: int = PATH_DOWNSAMPLE_MINUTES) -> list[dict]:
    return [
        {"ts": b.ts.isoformat(), "c": b.c}
        for i, b in enumerate(bars) if i % every_minutes == 0
    ]


def score_thesis(
    *,
    entry_premium: float,
    target_premium: float,
    stop_premium: float,
    lots: int,
    lot_size: int,
    charges: float,
    created_at: datetime,
    holidays: set[date],
    outcome: str,
    exit_time: datetime | None,
    exit_premium: float | None,
    ambiguous_bar: bool,
    full_window_bars: list[Bar],
    bars_quality: str,
    entry_spot: float,
    exit_spot: float | None,
    entry_sector_index: float | None,
    exit_sector_index: float | None,
    entry_market_index: float | None,
    exit_market_index: float | None,
    extra_flags: list[str] | None = None,
    planned_outcome: str | None = None,
    planned_exit_premium: float | None = None,
    scored_at: datetime,
) -> dict:
    """Returns a dict matching OptionThesisScore's columns (minus thesis_id),
    ready for db.repo.upsert_option_thesis_score(**result)."""
    if outcome == "unresolvable" or exit_time is None or exit_premium is None:
        return {
            "scored_at": scored_at, "score_version": SCORE_VERSION,
            "days_held": 0, "ret_pct": 0.0, "pnl": 0.0, "charges": 0.0, "r_multiple": 0.0,
            "mfe_pct": None, "mfe_time": None, "mae_pct": None, "mae_time": None,
            "target_after_stop": None, "planned_outcome": planned_outcome, "planned_r": None,
            "spot_ret_pct": None, "sector_ret_pct": None, "market_ret_pct": None,
            "bars": bars_quality, "flags": list(extra_flags or []), "path": [],
        }

    days_held = trading_days_between(created_at.date(), exit_time.date(), holidays)
    ret_pct = (exit_premium - entry_premium) / entry_premium
    pnl = (exit_premium - entry_premium) * lots * lot_size - charges
    r_multiple = (exit_premium - entry_premium) / (entry_premium - stop_premium)

    flags = list(extra_flags or [])
    if ambiguous_bar:
        flags.append("ambiguous_bar")

    planned_r = None
    if planned_outcome is not None and planned_exit_premium is not None:
        planned_r = (planned_exit_premium - entry_premium) / (entry_premium - stop_premium)

    return {
        "scored_at": scored_at,
        "score_version": SCORE_VERSION,
        "days_held": days_held,
        "ret_pct": ret_pct,
        "pnl": pnl,
        "charges": charges,
        "r_multiple": r_multiple,
        **_mfe_mae(entry_premium, full_window_bars),
        "target_after_stop": _target_after_stop(outcome, exit_time, target_premium, full_window_bars),
        "planned_outcome": planned_outcome,
        "planned_r": planned_r,
        "spot_ret_pct": _index_ret_pct(entry_spot, exit_spot),
        "sector_ret_pct": _index_ret_pct(entry_sector_index, exit_sector_index),
        "market_ret_pct": _index_ret_pct(entry_market_index, exit_market_index),
        "bars": bars_quality,
        "flags": flags,
        "path": _downsampled_path(full_window_bars),
    }
