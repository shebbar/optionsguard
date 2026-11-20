"""Section 7.9.11's isolation/idempotency guarantees that don't fit
naturally into the other option-theses test files: paper theses never
touch monthly P&L/cooloff, mock-data theses are never resolved or scored,
MCP can't reach exit/cancel/link/resolve, a stop-triggered GTT's reconciled
exit_reason maps to outcome=stop, and re-scoring with changed policy
config updates the stored policy_params."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from optionguard.config import get_config
from optionguard.db import repo
from optionguard.db.models import Check, OptionThesis
from optionguard.market import factory
from optionguard.option_theses.sync import run_option_theses_sync
from optionguard.pricing.calendar import trading_day_after
from optionguard.rules.guardrails import cooloff_status, monthly_realised_pnl


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
        created_at=repo.utcnow(), batch_id="isolation-test", symbol=symbol, expiry=expiry, strike=strike,
        opt_type=opt_type, data_source="kite", spot=spot, premium=premium, lot_size=1, verdict="green",
        flags=[], max_lots=5, raw={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _make_thesis(
    db, provider, *, offset, created_days_ago, duration_days, target_premium, stop_premium,
    mode="paper", data_source="kite",
) -> OptionThesis:
    holidays: set[date] = set(get_config().holidays.dates)
    today = date.today()
    spot = provider.spot("TCS")
    strike_guess = round(spot / 50) * 50 + 50 + offset
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
        opt_instrument_token=token, mode=mode, data_source=data_source, sector="IT", sector_index=None,
        why="isolation test fixture, not a real trade idea", check_id=check.id, verdict="green",
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


def test_paper_thesis_losses_dont_affect_monthly_pnl_or_cooloff(db, mock_mode):
    """monthly_realised_pnl/cooloff_status read only from `trades` --
    scoring a big paper-thesis loss must leave both untouched."""
    cfg = get_config()
    settings = repo.ensure_default_settings(db, cfg)
    thesis = _make_thesis(db, mock_mode, offset=100, created_days_ago=3, duration_days=2, target_premium=1_000_000.0, stop_premium=0.01)
    repo.upsert_option_thesis_score(
        db, thesis.id, scored_at=repo.utcnow(), score_version=1, days_held=2, ret_pct=-0.99, pnl=-999_999,
        charges=60, r_multiple=-50.0, mfe_pct=None, mfe_time=None, mae_pct=None, mae_time=None,
        target_after_stop=None, planned_outcome=None, planned_r=None, spot_ret_pct=None, sector_ret_pct=None,
        market_ret_pct=None, bars="minute", flags=[], path=[],
    )
    today = date.today()
    holidays = set(cfg.holidays.dates)
    assert monthly_realised_pnl(db, today) == 0.0
    in_cooloff, _ = cooloff_status(db, settings, today, holidays)
    assert in_cooloff is False


def test_mock_data_thesis_is_never_resolved_or_scored(db, mock_mode):
    cfg = get_config()
    thesis = _make_thesis(
        db, mock_mode, offset=150, created_days_ago=10, duration_days=5,
        target_premium=1_000_000.0, stop_premium=0.01, data_source="mock",
    )
    result = run_option_theses_sync(db, cfg)
    assert result["errors"] == []
    db.refresh(thesis)
    assert thesis.status == "open"  # never touched
    assert thesis.outcome is None
    assert repo.get_option_thesis_score(db, thesis.id) is None


def test_mcp_option_theses_tools_have_no_exit_cancel_link_or_resolve():
    import sys

    sys.path.insert(0, ".")
    from mcp_server.server import mcp

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    forbidden = {
        "resolve_option_thesis", "exit_option_thesis", "cancel_option_thesis", "link_option_thesis_trade",
        "option_thesis_exit", "option_thesis_cancel", "option_thesis_link_trade",
    }
    assert not (names & forbidden)
    assert {"preview_option_thesis", "add_option_thesis", "list_option_theses", "get_option_thesis", "option_thesis_scorecard"} <= names


def test_add_option_thesis_mcp_tool_always_sends_mode_paper(monkeypatch):
    import mcp_server.server as srv

    calls = []
    monkeypatch.setattr(srv, "_request", lambda method, path, *, json=None, params=None: calls.append((method, path, json)) or {"ok": True})
    srv.add_option_thesis(check_id=1, why="mcp isolation test", target_premium=60, stop_premium=30, duration_days=5)
    _, _, body = calls[0]
    assert body["mode"] == "paper"


def test_stop_triggered_gtt_reconciled_exit_maps_to_outcome_stop(db, mock_mode):
    """orders/service.py's reconcile() (tested independently elsewhere)
    closes a trade with exit_reason='premium_stop' once a GTT fires -- the
    real-thesis resolution step must map that to outcome='stop'."""
    cfg = get_config()
    thesis = _make_thesis(
        db, mock_mode, offset=200, created_days_ago=3, duration_days=2,
        target_premium=1_000_000.0, stop_premium=0.01, mode="real",
    )
    check = repo.get_check(db, thesis.check_id)
    trade = repo.create_trade(
        db, check_id=check.id, entry_time=thesis.created_at, lots=1, lot_size=1,
        entry_premium=float(thesis.entry_premium), stop_premium=float(thesis.stop_premium),
        thesis_stop_level=None, time_stop_date=date(2027, 1, 1),
    )
    repo.update_trade_order_fields(
        db, trade.id, status="closed", exit_time=repo.utcnow(), exit_premium=30.0, exit_reason="premium_stop",
    )
    repo.update_option_thesis(db, thesis.id, trade_id=trade.id)

    run_option_theses_sync(db, cfg)
    db.refresh(thesis)
    assert thesis.outcome == "stop"
    assert float(thesis.exit_premium) == 30.0


def test_rescoring_with_changed_disaster_stop_pct_records_the_new_param(db, mock_mode):
    cfg = get_config()
    thesis = _make_thesis(
        db, mock_mode, offset=250, created_days_ago=10, duration_days=5,
        target_premium=1_000_000.0, stop_premium=0.01,
    )
    run_option_theses_sync(db, cfg)
    first = repo.list_option_thesis_policy_results(db, thesis.id)
    grace3_before = next(r for r in first if r.policy == "grace3")
    assert grace3_before.policy_params["disaster_stop_pct"] == cfg.option_theses.policies.disaster_stop_pct

    original = cfg.option_theses.policies.disaster_stop_pct
    cfg.option_theses.policies.disaster_stop_pct = 0.75
    try:
        run_option_theses_sync(db, cfg)
    finally:
        cfg.option_theses.policies.disaster_stop_pct = original

    after = repo.list_option_thesis_policy_results(db, thesis.id)
    grace3_after = next(r for r in after if r.policy == "grace3")
    assert grace3_after.policy_params["disaster_stop_pct"] == 0.75
