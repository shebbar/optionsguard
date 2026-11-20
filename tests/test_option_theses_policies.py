"""Section 7.9.7/7.9.11: the 6-policy exit replay engine, against
hand-built fixture bars. Pure functions, no I/O."""
from __future__ import annotations

from datetime import date, datetime

from optionguard.option_theses.policies import (
    grace3_rules, hold_rules, plain_rules, r_multiple_for, replay_policy, time_checkpoint_rules,
)
from optionguard.option_theses.resolve import Bar, resolve_paper

ENTRY, TARGET, STOP = 50.0, 100.0, 30.0
ENTRY_DATE = date(2026, 1, 1)  # Thursday
HOLIDAYS: set[date] = set()

# Ten consecutive trading days from entry (skipping the weekend).
DAYS = [
    date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
    date(2026, 1, 8), date(2026, 1, 9), date(2026, 1, 12), date(2026, 1, 13), date(2026, 1, 14),
]


def _bar(d: date, c: float) -> Bar:
    return Bar(ts=datetime.combine(d, datetime.min.time()), o=c, h=c, l=c, c=c)


def test_plain_stop_but_grace3_survives_to_the_target():
    closes = [40, 28, 36, 40, 45, 110, 115]  # day2 dips past the normal stop, day6 reaches target
    bars = [_bar(d, c) for d, c in zip(DAYS, closes)]

    plain = replay_policy(ENTRY, TARGET, bars, ENTRY_DATE, HOLIDAYS, plain_rules(STOP), slippage_pct=0.0)
    grace3 = replay_policy(ENTRY, TARGET, bars, ENTRY_DATE, HOLIDAYS, grace3_rules(ENTRY, STOP, grace_days=3, disaster_stop_pct=0.6), slippage_pct=0.0)

    assert plain["outcome"] == "stop"
    assert grace3["outcome"] == "target"


def test_plain_matches_the_thesiss_own_resolution():
    closes = [40, 33, 36, 40, 45, 110, 115]
    bars = [_bar(d, c) for d, c in zip(DAYS, closes)]
    plain = replay_policy(ENTRY, TARGET, bars, ENTRY_DATE, HOLIDAYS, plain_rules(STOP), slippage_pct=0.0)
    resolved = resolve_paper(ENTRY, TARGET, STOP, bars, slippage_pct=0.0)
    assert plain["outcome"] == resolved["outcome"]
    assert plain["exit_time"] == resolved["exit_time"]
    assert plain["exit_premium"] == resolved["exit_premium"]


def test_grace3_disaster_has_a_larger_loss_than_plain():
    # disaster level = 50 x (1 - 0.6) = 20; day1's low dips through both the
    # normal stop (30) and the disaster level (20), with no gap at the open --
    # so each policy's own stop-hit level decides its exit price.
    bars = [Bar(ts=datetime.combine(DAYS[0], datetime.min.time()), o=48, h=50, l=15, c=20)]
    plain = replay_policy(ENTRY, TARGET, bars, ENTRY_DATE, HOLIDAYS, plain_rules(STOP), slippage_pct=0.0)
    grace3 = replay_policy(ENTRY, TARGET, bars, ENTRY_DATE, HOLIDAYS, grace3_rules(ENTRY, STOP, grace_days=3, disaster_stop_pct=0.6), slippage_pct=0.0)

    assert plain["outcome"] == "stop"
    assert grace3["outcome"] == "disaster"
    r_plain = r_multiple_for(ENTRY, STOP, plain["exit_premium"])
    r_grace3 = r_multiple_for(ENTRY, STOP, grace3["exit_premium"])
    assert r_grace3 < r_plain


def test_flat_decaying_path_time_d5_plain_and_hold_give_three_different_exits():
    closes = [48, 46, 44, 42, 40, 38, 36, 34, 32, 30]
    bars_to_end_date = [_bar(d, c) for d, c in zip(DAYS[:7], closes[:7])]  # end_date = day 7
    bars_to_hold_cutoff = [_bar(d, c) for d, c in zip(DAYS, closes)]  # hold cutoff = day 10

    plain = replay_policy(ENTRY, TARGET, bars_to_end_date, ENTRY_DATE, HOLIDAYS, plain_rules(STOP), slippage_pct=0.0)
    time_d5 = replay_policy(ENTRY, TARGET, bars_to_end_date, ENTRY_DATE, HOLIDAYS, time_checkpoint_rules(STOP, checkpoint_day=5, min_gain_pct=0.0), slippage_pct=0.0)
    hold = replay_policy(ENTRY, TARGET, bars_to_hold_cutoff, ENTRY_DATE, HOLIDAYS, hold_rules(), slippage_pct=0.0)

    assert time_d5["outcome"] == "time_checkpoint" and time_d5["exit_premium"] == 40
    assert plain["outcome"] == "time" and plain["exit_premium"] == 36
    assert hold["outcome"] == "hold_end" and hold["exit_premium"] == 30
    assert hold["exit_premium"] < plain["exit_premium"] < time_d5["exit_premium"]


def test_checkpoint_skipped_when_above_entry_times_one_plus_min_gain():
    closes = [52, 54, 56, 58, 60, 62, 64]  # stays above entry throughout
    bars = [_bar(d, c) for d, c in zip(DAYS[:7], closes)]
    time_d5 = replay_policy(ENTRY, TARGET, bars, ENTRY_DATE, HOLIDAYS, time_checkpoint_rules(STOP, checkpoint_day=5, min_gain_pct=0.0), slippage_pct=0.0)
    assert time_d5["outcome"] == "time"  # the checkpoint didn't fire; falls through to the plain time exit
    assert time_d5["exit_premium"] == 64


def test_r_multiple_for_uses_the_thesiss_own_planned_risk_for_every_policy():
    # grace3's disaster exit and hold's no-stop exit both still use (entry - stop_premium) as the risk,
    # not their own policy-specific level.
    assert r_multiple_for(ENTRY, STOP, exit_premium=20.0) == (20.0 - ENTRY) / (ENTRY - STOP)
    assert r_multiple_for(ENTRY, STOP, exit_premium=30.0) == (30.0 - ENTRY) / (ENTRY - STOP)
    assert r_multiple_for(ENTRY, STOP, exit_premium=None) is None
