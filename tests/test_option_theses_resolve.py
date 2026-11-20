"""Section 7.9.5/7.9.11: paper resolution from fixture bars, and the
manual-exit helpers. Pure functions, no I/O."""
from __future__ import annotations

from datetime import datetime

from optionguard.option_theses.resolve import (
    Bar, find_bar_at, is_late_manual_exit, resolve_paper, validate_manual_exit_price,
)

ENTRY, TARGET, STOP = 50.0, 70.0, 35.0


def _bar(minute: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(ts=datetime(2026, 1, 1, 9, 15 + minute), o=o, h=h, l=l, c=c)


def test_clean_target():
    bars = [_bar(0, 52, 55, 51, 54), _bar(1, 54, 72, 53, 71)]
    result = resolve_paper(ENTRY, TARGET, STOP, bars, slippage_pct=0.0)
    assert result["outcome"] == "target"
    assert result["exit_time"] == bars[1].ts
    assert result["exit_premium"] == TARGET
    assert result["ambiguous_bar"] is False


def test_clean_stop():
    bars = [_bar(0, 52, 53, 48, 50), _bar(1, 49, 50, 30, 33)]
    result = resolve_paper(ENTRY, TARGET, STOP, bars, slippage_pct=0.0)
    assert result["outcome"] == "stop"
    assert result["exit_premium"] == STOP


def test_time_exit_at_last_bars_close():
    bars = [_bar(0, 52, 55, 48, 51), _bar(1, 51, 54, 49, 52)]
    result = resolve_paper(ENTRY, TARGET, STOP, bars, slippage_pct=0.0)
    assert result["outcome"] == "time"
    assert result["exit_time"] == bars[-1].ts
    assert result["exit_premium"] == bars[-1].c


def test_gap_open_through_stop_exits_at_the_open_not_the_stop():
    bars = [_bar(0, 20, 20, 15, 18)]  # opens already below stop
    result = resolve_paper(ENTRY, TARGET, STOP, bars, slippage_pct=0.0)
    assert result["outcome"] == "stop"
    assert result["exit_premium"] == 20  # the open, not STOP (35)
    assert result["ambiguous_bar"] is False


def test_gap_open_through_target_exits_at_the_open_not_the_target():
    bars = [_bar(0, 90, 95, 88, 92)]
    result = resolve_paper(ENTRY, TARGET, STOP, bars, slippage_pct=0.0)
    assert result["outcome"] == "target"
    assert result["exit_premium"] == 90


def test_both_in_one_bar_is_stop_and_flagged_ambiguous():
    bars = [_bar(0, 52, 75, 30, 60)]  # high clears target, low clears stop, open is clean
    result = resolve_paper(ENTRY, TARGET, STOP, bars, slippage_pct=0.0)
    assert result["outcome"] == "stop"
    assert result["exit_premium"] == STOP
    assert result["ambiguous_bar"] is True


def test_slippage_applied_against_the_user_on_every_exit():
    bars = [_bar(0, 52, 55, 51, 54), _bar(1, 54, 72, 53, 71)]
    result = resolve_paper(ENTRY, TARGET, STOP, bars, slippage_pct=0.01)
    assert result["exit_premium"] == TARGET * 0.99


def test_resolves_as_soon_as_hit_not_waiting_for_the_last_bar():
    bars = [
        _bar(0, 52, 55, 51, 54),
        _bar(1, 54, 72, 53, 71),  # target hit here, bar 2 (below) must be ignored
        _bar(2, 71, 71, 10, 10),
    ]
    result = resolve_paper(ENTRY, TARGET, STOP, bars, slippage_pct=0.0)
    assert result["outcome"] == "target"
    assert result["exit_time"] == bars[1].ts


def test_no_bars_is_unresolvable():
    result = resolve_paper(ENTRY, TARGET, STOP, [], slippage_pct=0.0)
    assert result["outcome"] == "unresolvable"
    assert result["exit_time"] is None
    assert result["exit_premium"] is None


def test_find_bar_at_exact_timestamp():
    bars = [_bar(0, 50, 52, 49, 51), _bar(1, 51, 53, 50, 52)]
    assert find_bar_at(bars, bars[1].ts) is bars[1]
    assert find_bar_at(bars, datetime(2099, 1, 1)) is None


def test_validate_manual_exit_price_within_and_outside_bar_range():
    bar = _bar(0, 50, 55, 45, 52)
    assert validate_manual_exit_price(50.0, bar) is True
    assert validate_manual_exit_price(45.0, bar) is True
    assert validate_manual_exit_price(55.0, bar) is True
    assert validate_manual_exit_price(44.99, bar) is False
    assert validate_manual_exit_price(55.01, bar) is False


def test_is_late_manual_exit_flag():
    exit_time = datetime(2026, 1, 1, 10, 0)
    assert is_late_manual_exit(exit_time, datetime(2026, 1, 1, 10, 30), late_exit_flag_days=1) is False
    assert is_late_manual_exit(exit_time, datetime(2026, 1, 3, 10, 30), late_exit_flag_days=1) is True
