"""Section 7.9.10: the order ticket's thesis_id integration -- tighten-only
pre-fill, forced OCO, and linking trade_id/entry_premium on fill. Reuses
test_orders_service.py's mock-fill pattern (a BUY LIMIT at the ask fills
immediately)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from optionguard.db import repo
from optionguard.db.models import Check, OptionThesis
from optionguard.orders import service
from optionguard.rules.errors import RuleViolation

NOW = datetime.now(timezone.utc)


def _make_check(db, provider, *, offset=200, symbol="TCS"):
    from optionguard.market.mock_kite_client import monthly_expiries

    spot = provider.spot(symbol)
    strike = round(spot / 50) * 50 + offset
    expiry = monthly_expiries(date.today(), count=2)[1]
    quote = provider.option_quote(symbol, expiry, strike, "CE")
    check = Check(
        created_at=datetime.now(timezone.utc), batch_id="t", symbol=symbol, expiry=expiry, strike=strike,
        opt_type="CE", data_source="mock", spot=spot, premium=quote.last_price, bid=quote.bid, ask=quote.ask,
        lot_size=quote.lot_size, iv=None, hv20=None, hv_percentile=None, breakeven=None, breakeven_pct=None,
        expected_move_pct=None, prob_profit=None, prob_zero=None, prob_thesis=None, theta_per_day_lot=None,
        delta=None, daily_move_needed=None, stretch_5d_pct=None, stretch_20dma_pct=None, spread_pct=None,
        thesis_target=None, thesis_date=None, verdict="green", flags=[], max_lots=10, alternatives=None,
        oi_context=None, raw={},
    )
    db.add(check)
    db.commit()
    db.refresh(check)
    return check, quote


def _make_real_thesis(db, check, quote, *, lots=1, stop_premium=None, target_premium=None, status="open") -> OptionThesis:
    entry = quote.ask
    thesis = OptionThesis(
        created_at=repo.utcnow(), status=status, symbol=check.symbol, expiry=check.expiry, strike=check.strike,
        opt_type=check.opt_type, opt_instrument_token=None, mode="real", data_source="mock", sector="IT",
        sector_index=None, why="order integration test fixture, not a real trade idea", check_id=check.id,
        verdict="green", check_snapshot={}, lot_size=check.lot_size, lots=lots, entry_premium=entry,
        target_premium=target_premium if target_premium is not None else round(entry * 1.5, 2),
        stop_premium=stop_premium if stop_premium is not None else round(entry * 0.5, 2),
        duration_days=5, end_date=date(2026, 1, 10), reward_risk=1.0, spot_entry=quote.last_price,
        entry_flags=[],
    )
    db.add(thesis)
    db.commit()
    db.refresh(thesis)
    return thesis


def test_review_entry_prefills_stop_and_target_from_thesis(db, mock_mode, orders_enabled):
    check, quote = _make_check(db, mock_mode, offset=200)
    thesis = _make_real_thesis(db, check, quote, lots=1)
    intent = service.review_entry(
        db, orders_enabled, check=check, lots=1, price=quote.ask, now=NOW, thesis_id=thesis.id,
    )
    assert intent.option_thesis_id == thesis.id
    assert float(intent.stop_premium) == float(thesis.stop_premium)
    assert float(intent.target_premium) == float(thesis.target_premium)


def test_review_entry_rejects_more_lots_than_thesis(db, mock_mode, orders_enabled):
    check, quote = _make_check(db, mock_mode, offset=250)
    thesis = _make_real_thesis(db, check, quote, lots=1)
    with pytest.raises(RuleViolation) as exc:
        service.review_entry(db, orders_enabled, check=check, lots=2, price=quote.ask, now=NOW, thesis_id=thesis.id)
    assert exc.value.code == "THESIS_LOOSENED"


def test_review_entry_rejects_looser_stop_than_thesis(db, mock_mode, orders_enabled):
    check, quote = _make_check(db, mock_mode, offset=300)
    # settings' own floor is premium_stop_pct=0.40 (min stop = 0.6 x price) --
    # keep both values on the right side of *that* floor, so only the
    # thesis's own (tighter) stop is what trips the rejection here.
    thesis = _make_real_thesis(db, check, quote, lots=1, stop_premium=round(quote.ask * 0.75, 2))
    looser_stop = round(quote.ask * 0.65, 2)  # further from entry than the thesis's stop
    with pytest.raises(RuleViolation) as exc:
        service.review_entry(
            db, orders_enabled, check=check, lots=1, price=quote.ask, stop_premium=looser_stop, now=NOW,
            thesis_id=thesis.id,
        )
    assert exc.value.code == "THESIS_LOOSENED"


def test_review_entry_rejects_looser_target_than_thesis(db, mock_mode, orders_enabled):
    check, quote = _make_check(db, mock_mode, offset=350)
    thesis = _make_real_thesis(db, check, quote, lots=1, target_premium=round(quote.ask * 1.3, 2))
    looser_target = round(quote.ask * 1.6, 2)  # further from entry than the thesis's target
    with pytest.raises(RuleViolation) as exc:
        service.review_entry(
            db, orders_enabled, check=check, lots=1, price=quote.ask, target_premium=looser_target, now=NOW,
            thesis_id=thesis.id,
        )
    assert exc.value.code == "THESIS_LOOSENED"


def test_review_entry_rejects_paper_or_already_linked_or_mismatched_thesis(db, mock_mode, orders_enabled):
    check, quote = _make_check(db, mock_mode, offset=400)

    paper_thesis = OptionThesis(
        created_at=repo.utcnow(), status="open", symbol=check.symbol, expiry=check.expiry, strike=check.strike,
        opt_type=check.opt_type, opt_instrument_token=None, mode="paper", data_source="mock", sector="IT",
        sector_index=None, why="order integration test fixture, not a real trade idea", check_id=check.id,
        verdict="green", check_snapshot={}, lot_size=check.lot_size, lots=1, entry_premium=quote.ask,
        target_premium=round(quote.ask * 1.5, 2), stop_premium=round(quote.ask * 0.5, 2), duration_days=5,
        end_date=date(2026, 1, 10), reward_risk=1.0, spot_entry=quote.last_price, entry_flags=[],
    )
    db.add(paper_thesis)
    db.commit()
    with pytest.raises(RuleViolation) as exc:
        service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask, now=NOW, thesis_id=paper_thesis.id)
    assert exc.value.code == "THESIS_NOT_OPEN"

    other_check, other_quote = _make_check(db, mock_mode, offset=450)
    mismatched_thesis = _make_real_thesis(db, other_check, other_quote, lots=1)
    with pytest.raises(RuleViolation) as exc:
        service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask, now=NOW, thesis_id=mismatched_thesis.id)
    assert exc.value.code == "THESIS_CONTRACT_MISMATCH"


def test_fill_links_trade_and_updates_entry_premium(db, mock_mode, orders_enabled):
    check, quote = _make_check(db, mock_mode, offset=500)
    thesis = _make_real_thesis(db, check, quote, lots=1)
    intent = service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask, now=NOW, thesis_id=thesis.id)
    confirmed = service.confirm_entry(db, orders_enabled, intent=intent, now=NOW)
    assert confirmed.status == "filled"

    trade = repo.list_open_trades(db)[0]
    db.refresh(thesis)
    assert thesis.trade_id == trade.id
    assert float(thesis.entry_premium) == quote.ask
    # the GTT must be OCO -- both a stop and a target leg.
    assert trade.gtt_status == "active"


def test_review_entry_rejects_an_already_linked_thesis(db, mock_mode, orders_enabled):
    """Isolates THESIS_ALREADY_LINKED from the (always-also-true, since a
    filled order also opens a trade on the same contract) TRADE_ALREADY_OPEN
    guard: link the thesis to a *closed* trade directly, so the generic
    open-trade guard doesn't fire first."""
    check, quote = _make_check(db, mock_mode, offset=500)
    thesis = _make_real_thesis(db, check, quote, lots=1)
    closed_trade = repo.create_trade(
        db, check_id=check.id, entry_time=repo.utcnow(), lots=1, lot_size=check.lot_size,
        entry_premium=quote.ask, stop_premium=round(quote.ask * 0.6, 2), thesis_stop_level=None,
        time_stop_date=date(2026, 1, 1),
    )
    repo.update_trade_order_fields(db, closed_trade.id, status="closed", exit_time=repo.utcnow(), exit_premium=quote.ask)
    thesis.trade_id = closed_trade.id
    db.commit()

    with pytest.raises(RuleViolation) as exc:
        service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask, now=NOW, thesis_id=thesis.id)
    assert exc.value.code == "THESIS_ALREADY_LINKED"


def test_order_page_prefills_from_a_linked_thesis(client, mock_mode, db, orders_enabled):
    check, quote = _make_check(db, mock_mode, offset=-50)
    thesis = _make_real_thesis(db, check, quote, lots=1)

    page = client.get(f"/order?check_id={check.id}&thesis_id={thesis.id}")
    assert page.status_code == 200
    assert f'name="thesis_id" value="{thesis.id}"' in page.text
    assert f'name="lots"' in page.text and f'value="{thesis.lots}"' in page.text
    assert f'value="{float(thesis.stop_premium)}"' in page.text
    assert f'value="{float(thesis.target_premium)}"' in page.text
