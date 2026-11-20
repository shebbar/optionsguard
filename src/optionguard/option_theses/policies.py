"""Section 7.9.7: replaying a thesis's bars under several exit policies, to
see whether a different rule would have done better than the thesis's own
(the `plain` policy, which always matches 7.9.5/7.9.6's own resolution).
Pure functions, no I/O -- orchestration (which bars to fetch, pending/
unresolvable bookkeeping for `hold`) lives in option_theses/sync.py."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from optionguard.option_theses.resolve import Bar
from optionguard.pricing.calendar import trading_days_between

POLICY_NAMES = ("plain", "grace3", "time_d5", "time_d8", "time_d11", "hold")


def day_of(entry_date: date, bar_date: date, holidays: set[date]) -> int:
    """1-indexed trading-day number, entry day = day 1."""
    return trading_days_between(entry_date, bar_date, holidays) + 1


@dataclass(slots=True)
class PolicyRules:
    name: str
    # day_number -> (stop_level, outcome_label), or None if no stop is active that day.
    stop_for_day: Callable[[int], tuple[float, str] | None]
    # day_number -> min_gain_pct checked at that day's close; only one day per policy in this build.
    time_exit_days: dict[int, float] = field(default_factory=dict)
    fallback_outcome: str = "time"  # label used when no stop/target/checkpoint fires


def plain_rules(stop_premium: float) -> PolicyRules:
    return PolicyRules(name="plain", stop_for_day=lambda _day: (stop_premium, "stop"))


def grace3_rules(entry_premium: float, stop_premium: float, grace_days: int, disaster_stop_pct: float) -> PolicyRules:
    disaster_level = entry_premium * (1 - disaster_stop_pct)

    def stop_for_day(day: int) -> tuple[float, str]:
        if day <= grace_days:
            return (disaster_level, "disaster")
        return (stop_premium, "stop")

    return PolicyRules(name="grace3", stop_for_day=stop_for_day)


def time_checkpoint_rules(stop_premium: float, checkpoint_day: int, min_gain_pct: float) -> PolicyRules:
    return PolicyRules(
        name=f"time_d{checkpoint_day}", stop_for_day=lambda _day: (stop_premium, "stop"),
        time_exit_days={checkpoint_day: min_gain_pct},
    )


def hold_rules() -> PolicyRules:
    return PolicyRules(name="hold", stop_for_day=lambda _day: None, fallback_outcome="hold_end")


def replay_policy(
    entry_premium: float, target_premium: float, bars: list[Bar], entry_date: date, holidays: set[date],
    rules: PolicyRules, slippage_pct: float,
) -> dict:
    """Walks `bars` (already trimmed to this policy's own window -- entry
    through end_date for plain/grace3/time_dN, entry through the hold
    cutoff date for hold) and returns the first stop/target/checkpoint hit,
    or the fallback outcome at the last bar's close. Mirrors 7.9.5's fill
    rules exactly: gaps exit at the open, stop-and-target in one bar counts
    as the stop (or disaster), slippage applies to every exit."""
    if not bars:
        return {"outcome": "unresolvable", "exit_time": None, "exit_premium": None}

    last_bar_of_day: dict[int, Bar] = {}
    for b in bars:
        last_bar_of_day[day_of(entry_date, b.ts.date(), holidays)] = b

    def exit_at(outcome: str, ts, raw_price: float) -> dict:
        return {"outcome": outcome, "exit_time": ts, "exit_premium": raw_price * (1 - slippage_pct)}

    for bar in bars:
        day = day_of(entry_date, bar.ts.date(), holidays)
        stop_info = rules.stop_for_day(day)

        if stop_info is not None and bar.o <= stop_info[0]:
            return exit_at(stop_info[1], bar.ts, bar.o)
        if bar.o >= target_premium:
            return exit_at("target", bar.ts, bar.o)

        stop_hit = stop_info is not None and bar.l <= stop_info[0]
        target_hit = bar.h >= target_premium
        if stop_hit and target_hit:
            return exit_at(stop_info[1], bar.ts, stop_info[0])
        if stop_hit:
            return exit_at(stop_info[1], bar.ts, stop_info[0])
        if target_hit:
            return exit_at("target", bar.ts, target_premium)

        if day in rules.time_exit_days and last_bar_of_day.get(day) is bar:
            min_gain = rules.time_exit_days[day]
            if bar.c < entry_premium * (1 + min_gain):
                return exit_at("time_checkpoint", bar.ts, bar.c)

    last = bars[-1]
    return exit_at(rules.fallback_outcome, last.ts, last.c)


def r_multiple_for(entry_premium: float, stop_premium: float, exit_premium: float | None) -> float | None:
    """7.9.7: R always uses the thesis's own planned risk (entry -
    stop_premium), in every policy -- including grace3 (whose disaster
    stop is a different level) and hold (which has no stop at all)."""
    if exit_premium is None:
        return None
    return (exit_premium - entry_premium) / (entry_premium - stop_premium)
