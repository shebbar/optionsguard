"""Section 7.9.6/7.9.11: per-thesis scoring. Pure function, no I/O."""
from __future__ import annotations

from datetime import date, datetime

from optionguard.option_theses.resolve import Bar
from optionguard.option_theses.scoring import score_thesis

ENTRY, TARGET, STOP = 50.0, 70.0, 35.0
HOLIDAYS: set[date] = set()
CREATED_AT = datetime(2026, 1, 1, 9, 20)
SCORED_AT = datetime(2026, 1, 10, 16, 0)


def _bar(d: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(ts=datetime(2026, 1, d, 15, 29), o=o, h=h, l=l, c=c)


def _base_kwargs(**overrides) -> dict:
    kwargs = dict(
        entry_premium=ENTRY, target_premium=TARGET, stop_premium=STOP, lots=1, lot_size=175, charges=60,
        created_at=CREATED_AT, holidays=HOLIDAYS, outcome="stop", exit_time=datetime(2026, 1, 2, 9, 16),
        exit_premium=STOP, ambiguous_bar=False, full_window_bars=[], bars_quality="minute",
        entry_spot=2500.0, exit_spot=2470.0, entry_sector_index=None, exit_sector_index=None,
        entry_market_index=None, exit_market_index=None, extra_flags=None, planned_outcome=None,
        planned_exit_premium=None, scored_at=SCORED_AT,
    )
    kwargs.update(overrides)
    return kwargs


def test_target_after_stop_is_detected():
    bars = [
        _bar(1, 50, 52, 34, 36),  # the stop fires here
        _bar(2, 36, 50, 35, 48),
        _bar(3, 48, 75, 47, 72),  # the target is later reached before end_date
    ]
    result = score_thesis(**_base_kwargs(exit_time=bars[0].ts, exit_premium=STOP, full_window_bars=bars))
    assert result["target_after_stop"] is True


def test_target_after_stop_is_false_when_the_target_never_comes():
    bars = [_bar(1, 50, 52, 34, 36), _bar(2, 36, 45, 30, 40)]
    result = score_thesis(**_base_kwargs(exit_time=bars[0].ts, exit_premium=STOP, full_window_bars=bars))
    assert result["target_after_stop"] is False


def test_mfe_and_mae_over_the_full_window():
    bars = [_bar(1, 50, 65, 48, 60), _bar(2, 60, 62, 20, 55)]
    result = score_thesis(**_base_kwargs(full_window_bars=bars))
    assert result["mfe_pct"] == (65 - ENTRY) / ENTRY
    assert result["mae_pct"] == (20 - ENTRY) / ENTRY


def test_r_multiple_and_pnl_are_exact():
    result = score_thesis(**_base_kwargs(outcome="target", exit_premium=TARGET))
    assert result["r_multiple"] == (TARGET - ENTRY) / (ENTRY - STOP)
    assert result["pnl"] == (TARGET - ENTRY) * 1 * 175 - 60


def test_unresolvable_short_circuits_to_a_zeroed_row():
    result = score_thesis(**_base_kwargs(outcome="unresolvable", exit_time=None, exit_premium=None))
    assert result["pnl"] == 0.0
    assert result["r_multiple"] == 0.0
    assert result["bars"] == "minute"


def test_ambiguous_bar_and_daily_fallback_flags():
    result = score_thesis(**_base_kwargs(ambiguous_bar=True, extra_flags=["daily_fallback"], bars_quality="daily_fallback"))
    assert "ambiguous_bar" in result["flags"]
    assert "daily_fallback" in result["flags"]
    assert result["bars"] == "daily_fallback"


def test_planned_vs_actual_computed_when_the_user_moved_the_stop():
    """Real mode: the bars say it would have stopped out, but the user
    actually exited manually at a better price (a moved stop)."""
    result = score_thesis(**_base_kwargs(
        outcome="manual", exit_premium=45.0, planned_outcome="stop", planned_exit_premium=STOP,
    ))
    assert result["planned_outcome"] == "stop"
    assert result["planned_r"] == (STOP - ENTRY) / (ENTRY - STOP)
    assert result["r_multiple"] == (45.0 - ENTRY) / (ENTRY - STOP)
    assert result["r_multiple"] != result["planned_r"]


def test_spot_sector_market_ret_pct():
    result = score_thesis(**_base_kwargs(
        entry_spot=2500.0, exit_spot=2550.0, entry_sector_index=1000.0, exit_sector_index=1010.0,
        entry_market_index=20000.0, exit_market_index=19800.0,
    ))
    assert result["spot_ret_pct"] == (2550.0 - 2500.0) / 2500.0
    assert result["sector_ret_pct"] == (1010.0 - 1000.0) / 1000.0
    assert result["market_ret_pct"] == (19800.0 - 20000.0) / 20000.0
