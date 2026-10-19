from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from optionguard.db import repo
from optionguard.rules import guardrails
from optionguard.rules.errors import RuleViolation

HOLIDAYS: set[date] = set()


def _settings(db, **overrides):
    defaults = dict(
        max_loss_per_trade=5000, monthly_loss_limit=10000, premium_stop_pct=0.4,
        time_stop_days=7, cooloff_days=10, show_spreads=False,
    )
    defaults.update(overrides)
    return repo.insert_settings(db, effective_from=datetime.now(timezone.utc), **defaults)


def _check(db, **overrides):
    defaults = dict(
        batch_id="b1", symbol="TCS", expiry=date(2026, 11, 26), strike=2500, opt_type="CE",
        data_source="manual", spot=2480, premium=35, lot_size=175, iv=0.25, flags=[],
        max_lots=2, raw={}, verdict="green",
    )
    defaults.update(overrides)
    return repo.create_check(db, **defaults)


def _closed_trade(db, check, *, exit_time: datetime, pnl: float):
    trade = repo.create_trade(
        db, check_id=check.id, entry_time=exit_time - timedelta(days=1), lots=1, lot_size=check.lot_size,
        entry_premium=35, stop_premium=20, time_stop_date=check.expiry,
    )
    trade.status = "closed"
    trade.exit_time = exit_time
    trade.exit_premium = 10
    trade.pnl = pnl
    db.commit()
    return trade


def test_monthly_realised_pnl_sums_closed_trades_this_month(db):
    check = _check(db)
    today = date.today()
    _closed_trade(db, check, exit_time=datetime(today.year, today.month, 5, tzinfo=timezone.utc), pnl=-2000)
    _closed_trade(db, check, exit_time=datetime(today.year, today.month, 10, tzinfo=timezone.utc), pnl=-3000)
    assert guardrails.monthly_realised_pnl(db, today) == -5000


def test_cooloff_triggers_after_monthly_limit_hit(db):
    settings = _settings(db, monthly_loss_limit=5000, cooloff_days=10)
    check = _check(db)
    today = date.today()
    trigger_day = datetime(today.year, today.month, 5, tzinfo=timezone.utc)
    _closed_trade(db, check, exit_time=trigger_day, pnl=-6000)

    in_cooloff, trigger_date = guardrails.cooloff_status(db, settings, today, HOLIDAYS)
    assert in_cooloff is True
    assert trigger_date == trigger_day.date()


def test_not_in_cooloff_when_pnl_above_limit(db):
    settings = _settings(db, monthly_loss_limit=5000)
    check = _check(db)
    today = date.today()
    _closed_trade(db, check, exit_time=datetime(today.year, today.month, 5, tzinfo=timezone.utc), pnl=-1000)
    in_cooloff, _ = guardrails.cooloff_status(db, settings, today, HOLIDAYS)
    assert in_cooloff is False


def test_record_trade_rejects_over_max_lots(db):
    settings = _settings(db)
    check = _check(db, max_lots=2, verdict="green")
    with pytest.raises(RuleViolation) as exc_info:
        guardrails.guard_record_trade(db, check, 3, settings, datetime.now(timezone.utc), HOLIDAYS, None)
    assert exc_info.value.code == "OVER_MAX_LOTS"
    assert exc_info.value.overridable is False


def test_record_trade_rejects_stale_check(db):
    settings = _settings(db)
    check = _check(db, max_lots=2, verdict="green")
    stale_now = check.created_at + timedelta(minutes=45)
    with pytest.raises(RuleViolation) as exc_info:
        guardrails.guard_record_trade(db, check, 1, settings, stale_now, HOLIDAYS, None)
    assert exc_info.value.code == "STALE_CHECK"


def test_record_trade_rejects_red_verdict_without_reason(db):
    settings = _settings(db)
    check = _check(db, max_lots=2, verdict="red")
    with pytest.raises(RuleViolation) as exc_info:
        guardrails.guard_record_trade(db, check, 1, settings, datetime.now(timezone.utc), HOLIDAYS, None)
    assert exc_info.value.code == "RED_VERDICT"
    assert exc_info.value.overridable is True


def test_record_trade_accepts_red_verdict_with_reason(db):
    settings = _settings(db)
    check = _check(db, max_lots=2, verdict="red")
    guardrails.guard_record_trade(
        db, check, 1, settings, datetime.now(timezone.utc), HOLIDAYS, "I have a strong independent thesis here"
    )  # no raise


def test_tighten_accepts_only_protective_premium_change():
    guardrails.guard_tighten_stop(
        old_stop_premium=20, new_stop_premium=25, old_time_stop_date=date(2026, 11, 20),
    )  # higher stop_premium is tighter -- no raise

    with pytest.raises(RuleViolation) as exc_info:
        guardrails.guard_tighten_stop(
            old_stop_premium=20, new_stop_premium=15, old_time_stop_date=date(2026, 11, 20),
        )
    assert exc_info.value.code == "NOT_TIGHTER"


def test_tighten_accepts_only_earlier_time_stop():
    guardrails.guard_tighten_stop(
        old_stop_premium=20, old_time_stop_date=date(2026, 11, 20), new_time_stop_date=date(2026, 11, 15),
    )  # no raise

    with pytest.raises(RuleViolation):
        guardrails.guard_tighten_stop(
            old_stop_premium=20, old_time_stop_date=date(2026, 11, 20), new_time_stop_date=date(2026, 11, 25),
        )


def test_tighten_rejects_no_change():
    with pytest.raises(RuleViolation) as exc_info:
        guardrails.guard_tighten_stop(old_stop_premium=20, old_time_stop_date=date(2026, 11, 20))
    assert exc_info.value.code == "NO_CHANGE"


def test_loosen_requires_long_reason():
    with pytest.raises(RuleViolation):
        guardrails.guard_loosen_stop("too short")
    guardrails.guard_loosen_stop("this reason is definitely long enough")  # no raise


def test_settings_loosening_delayed_to_next_trading_day(db):
    old = _settings(db, max_loss_per_trade=5000)
    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)  # a Tuesday
    effective = guardrails.settings_effective_from(old, {"max_loss_per_trade": 10000}, now, HOLIDAYS)
    assert effective.date() == date(2026, 11, 11)
    assert effective.hour == 9


def test_settings_tightening_effective_immediately(db):
    old = _settings(db, max_loss_per_trade=5000)
    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    effective = guardrails.settings_effective_from(old, {"max_loss_per_trade": 2000}, now, HOLIDAYS)
    assert effective == now
