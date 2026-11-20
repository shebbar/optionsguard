"""Section 7.9.6: the bars -> resolve -> score -> policies pipeline,
end to end against the mock provider. Mirrors test_theses_sync.py's
pattern of backdating created_at directly on the model (repo.create_*
always stamps created_at=now()) so a thesis can look "aged out" without
waiting on wall-clock time."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from optionguard.config import get_config
from optionguard.db import repo
from optionguard.db.models import Check, OptionThesis
from optionguard.market import factory
from optionguard.option_theses.sync import run_option_theses_sync
from optionguard.pricing.calendar import trading_day_after, trading_day_on_or_before


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setattr(factory, "is_kite_mock", lambda: True)
    factory.get_kite_client.cache_clear()
    return factory.get_kite_provider()


def _next_monthly_expiry(nth: int = 2):
    from optionguard.market.mock_kite_client import monthly_expiries

    return monthly_expiries(date.today(), count=nth)[nth - 1]


def _make_check(db, *, symbol, expiry, strike, opt_type, spot, premium) -> Check:
    row = Check(
        created_at=repo.utcnow(), batch_id="sync-test", symbol=symbol, expiry=expiry, strike=strike,
        opt_type=opt_type, data_source="kite", spot=spot, premium=premium, lot_size=1, verdict="green",
        flags=[], max_lots=5, raw={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _make_thesis(
    db, provider, *, offset, created_days_ago, duration_days, target_premium, stop_premium,
    expiry=None, mode="paper",
) -> OptionThesis:
    holidays: set[date] = set(get_config().holidays.dates)
    today = date.today()
    spot = provider.spot("TCS")
    strike_guess = round(spot / 50) * 50 + 50 + offset
    if expiry is None:
        expiry = _next_monthly_expiry(2)

    strikes = sorted(provider.list_strikes("TCS", expiry))
    strike = min(strikes, key=lambda s: abs(s - strike_guess))
    idx = strikes.index(strike)
    strike = strikes[(idx + (offset // 50)) % len(strikes)]
    token = provider.option_instrument_token("TCS", expiry, strike, "CE")

    entry_date = today - timedelta(days=created_days_ago)
    created_at = datetime.combine(entry_date, datetime.min.time(), tzinfo=timezone.utc)
    end_date = trading_day_after(entry_date, duration_days, holidays)
    entry_premium, _iv = provider.client._option_premium("TCS", strike, expiry, "CE", as_of=entry_date)

    check = _make_check(db, symbol="TCS", expiry=expiry, strike=strike, opt_type="CE", spot=spot, premium=entry_premium)

    thesis = OptionThesis(
        created_at=created_at, status="open", symbol="TCS", expiry=expiry, strike=strike, opt_type="CE",
        opt_instrument_token=token, mode=mode, data_source="kite", sector="IT", sector_index=None,
        why="sync pipeline test fixture, not a real trade idea", check_id=check.id, verdict="green",
        check_snapshot={"spot": spot}, lot_size=1, lots=1, entry_premium=round(entry_premium, 2),
        target_premium=target_premium, stop_premium=stop_premium, duration_days=duration_days, end_date=end_date,
        reward_risk=1.0, spot_for_target=None, spot_for_stop=None, mkt_prob_target_first=None,
        mkt_prob_stop_first=None, mkt_prob_time_exit=None, spot_entry=spot, sector_index_entry=None,
        market_index_entry=None, entry_flags=[],
    )
    db.add(thesis)
    db.commit()
    db.refresh(thesis)
    return thesis


def test_unreachable_target_and_stop_resolves_as_time_exit_and_scores(db, mock_mode):
    """entry far below target and far above stop: neither can fire, so the
    thesis must resolve as a time exit at end_date's close, get scored, and
    get `plain`/`grace3`/`time_dN` policy rows -- all deterministic, with
    no dependence on the mock's random walk."""
    cfg = get_config()
    thesis = _make_thesis(
        db, mock_mode, offset=100, created_days_ago=10, duration_days=5,
        target_premium=1_000_000.0, stop_premium=0.01,
    )

    result = run_option_theses_sync(db, cfg)
    assert result["errors"] == [], result["errors"]
    assert result["counts"]["option_bars_upserted"] > 0
    assert result["counts"]["option_theses_resolved"] >= 1
    assert result["counts"]["option_theses_scored"] >= 1
    assert result["counts"]["option_policies_computed"] >= 1

    db.refresh(thesis)
    assert thesis.status == "scored"
    assert thesis.outcome == "time"
    assert thesis.exit_time.date() == thesis.end_date

    score = repo.get_option_thesis_score(db, thesis.id)
    assert score is not None
    assert score.bars == "minute"
    assert score.days_held == 5

    results = {r.policy: r for r in repo.list_option_thesis_policy_results(db, thesis.id)}
    assert results["plain"].outcome == "time"
    assert results["plain"].pending is False
    assert results["grace3"].outcome == "time"
    for n in cfg.option_theses.policies.time_checkpoints:
        if n <= thesis.duration_days:
            assert results[f"time_d{n}"].outcome in ("time", "time_checkpoint")


def test_hold_policy_is_pending_when_bars_dont_reach_the_cutoff(db, mock_mode):
    """A thesis whose contract expiry is weeks out: the hold cutoff date is
    far in the future, so bars fetched "to now" can't reach it yet --
    `hold` must come back pending, not finalized at some arbitrary partial
    close."""
    cfg = get_config()
    thesis = _make_thesis(
        db, mock_mode, offset=110, created_days_ago=10, duration_days=5,
        target_premium=1_000_000.0, stop_premium=0.01,
    )
    holidays = set(cfg.holidays.dates)
    hold_cutoff = trading_day_on_or_before(
        thesis.expiry - timedelta(days=cfg.option_theses.hold_cutoff_calendar_days), holidays,
    )
    assert hold_cutoff > date.today(), "fixture assumption: hold cutoff must still be in the future"

    run_option_theses_sync(db, cfg)

    results = {r.policy: r for r in repo.list_option_thesis_policy_results(db, thesis.id)}
    assert results["hold"].pending is True
    assert results["hold"].outcome is None
    assert results["plain"].pending is False


def test_second_sync_is_idempotent(db, mock_mode):
    cfg = get_config()
    thesis = _make_thesis(
        db, mock_mode, offset=120, created_days_ago=10, duration_days=5,
        target_premium=1_000_000.0, stop_premium=0.01,
    )
    run_option_theses_sync(db, cfg)
    db.refresh(thesis)
    first_exit_time = thesis.exit_time

    result2 = run_option_theses_sync(db, cfg)
    assert result2["errors"] == []
    db.refresh(thesis)
    assert thesis.exit_time == first_exit_time
    assert thesis.status == "scored"


def test_real_thesis_unlinked_past_entry_day_is_auto_cancelled(db, mock_mode):
    cfg = get_config()
    thesis = _make_thesis(
        db, mock_mode, offset=130, created_days_ago=3, duration_days=2,
        target_premium=1_000_000.0, stop_premium=0.01, mode="real",
    )
    run_option_theses_sync(db, cfg)
    db.refresh(thesis)
    assert thesis.status == "cancelled"
    assert thesis.cancel_reason == "no trade linked by the end of its entry day"
