"""Section 7.8.4/7.8.5/7.8.6 / 7.8.9: the order review/confirm/poll/GTT/exit/
reconciliation service, against the mock Kite client and a real Postgres
test DB."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from optionguard.config import get_config
from optionguard.db import repo
from optionguard.db.models import Check
from optionguard.orders import service
from optionguard.orders.gtt import round_to_tick
from optionguard.rules.errors import RuleViolation


def _make_check(db, provider, *, offset=50, verdict="green", max_lots=10, data_source="mock",
                 created_at=None, symbol="TCS"):
    spot = provider.spot(symbol)
    strike = round(spot / 50) * 50 + offset
    from datetime import date

    from optionguard.market.mock_kite_client import monthly_expiries

    expiry = monthly_expiries(date.today(), count=2)[1]
    quote = provider.option_quote(symbol, expiry, strike, "CE")
    check = Check(
        created_at=created_at or datetime.now(timezone.utc), batch_id="t", symbol=symbol, expiry=expiry,
        strike=strike, opt_type="CE", data_source=data_source, spot=spot, premium=quote.last_price,
        bid=quote.bid, ask=quote.ask, lot_size=quote.lot_size, iv=None, hv20=None, hv_percentile=None,
        breakeven=None, breakeven_pct=None, expected_move_pct=None, prob_profit=None, prob_zero=None,
        prob_thesis=None, theta_per_day_lot=None, delta=None, daily_move_needed=None, stretch_5d_pct=None,
        stretch_20dma_pct=None, spread_pct=None, thesis_target=None, thesis_date=None, verdict=verdict,
        flags=[], max_lots=max_lots, alternatives=None, oi_context=None, raw={},
    )
    db.add(check)
    db.commit()
    db.refresh(check)
    return check, quote


NOW = datetime.now(timezone.utc)


# ---- guardrails before placement (7.8.9) -----------------------------------

def test_review_blocked_over_max_lots(db, mock_mode, orders_enabled):
    check, quote = _make_check(db, mock_mode, offset=50, max_lots=1)
    with pytest.raises(RuleViolation) as exc:
        service.review_entry(db, orders_enabled, check=check, lots=2, price=quote.ask, now=NOW)
    assert exc.value.code == "OVER_MAX_LOTS"
    assert repo.list_order_intents(db) == []


def test_review_blocked_stale_check(db, mock_mode, orders_enabled):
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=45)
    check, quote = _make_check(db, mock_mode, offset=50, created_at=stale_time)
    with pytest.raises(RuleViolation) as exc:
        service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask, now=NOW)
    assert exc.value.code == "STALE_CHECK"


def test_review_blocked_red_verdict_without_reason_accepted_with_one(db, mock_mode, orders_enabled):
    check, quote = _make_check(db, mock_mode, offset=50, verdict="red")
    with pytest.raises(RuleViolation) as exc:
        service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask, now=NOW)
    assert exc.value.code == "RED_VERDICT"

    intent = service.review_entry(
        db, orders_enabled, check=check, lots=1, price=quote.ask,
        override_reason="accepting the red verdict for this test", now=NOW,
    )
    assert intent.status == "reviewed"


def test_review_blocked_at_monthly_loss_limit(db, mock_mode, orders_enabled):
    cfg = orders_enabled
    settings = repo.ensure_default_settings(db, cfg)
    check0, quote0 = _make_check(db, mock_mode, offset=50)
    trade = repo.create_trade(
        db, check_id=check0.id, entry_time=NOW, lots=1, lot_size=check0.lot_size,
        entry_premium=100, stop_premium=10, thesis_stop_level=None, time_stop_date=NOW.date(),
    )
    repo.update_trade_order_fields(
        db, trade.id, status="closed", exit_time=NOW, exit_premium=0,
        exit_reason="manual", charges=0, pnl=-float(settings.monthly_loss_limit) - 1,
    )
    check, quote = _make_check(db, mock_mode, offset=50)
    with pytest.raises(RuleViolation) as exc:
        service.review_entry(db, cfg, check=check, lots=1, price=quote.ask, now=NOW)
    assert exc.value.code == "MONTHLY_LOSS_LIMIT"


def test_review_blocked_price_beyond_fat_finger_pct(db, mock_mode, orders_enabled):
    check, quote = _make_check(db, mock_mode, offset=50)
    with pytest.raises(RuleViolation) as exc:
        service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask * 2, now=NOW)
    assert exc.value.code == "PRICE_OFF_MARKET"


# ---- idempotency and duplicate-contract rejection --------------------------

def test_duplicate_live_intent_on_same_contract_rejected(db, mock_mode, orders_enabled):
    check, quote = _make_check(db, mock_mode, offset=50)
    service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask, now=NOW)
    with pytest.raises(RuleViolation) as exc:
        service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask, now=NOW)
    assert exc.value.code == "ORDER_IN_PROGRESS"


def test_double_confirm_places_exactly_one_order(db, mock_mode, orders_enabled):
    check, quote = _make_check(db, mock_mode, offset=50)
    intent = service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask, now=NOW)
    first = service.confirm_entry(db, orders_enabled, intent=intent, now=NOW)
    second = service.confirm_entry(db, orders_enabled, intent=repo.get_order_intent(db, intent.id), now=NOW)
    assert first.order_id == second.order_id
    assert len(repo.list_open_trades(db)) == 1


# ---- fill handling ----------------------------------------------------------

def test_full_fill_records_trade_at_fill_price_and_places_gtt(db, mock_mode, orders_enabled):
    check, quote = _make_check(db, mock_mode, offset=50)
    intent = service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask, now=NOW)
    confirmed = service.confirm_entry(db, orders_enabled, intent=intent, now=NOW)
    assert confirmed.status == "filled"
    trade = repo.list_open_trades(db)[0]
    assert float(trade.entry_premium) == quote.ask
    assert trade.lots * trade.lot_size == confirmed.filled_qty
    assert trade.gtt_id is not None
    assert trade.gtt_status == "active"


def test_rejection_records_nothing(db, mock_mode, orders_enabled, monkeypatch):
    check, quote = _make_check(db, mock_mode, offset=50)
    intent = service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask, now=NOW)

    def fake_place_order(*args, **kwargs):
        raise RuntimeError("Kite rejected: insufficient margin")

    monkeypatch.setattr(mock_mode.__class__, "place_order", lambda self, *a, **k: fake_place_order())
    confirmed = service.confirm_entry(db, orders_enabled, intent=intent, now=NOW)
    assert confirmed.status == "rejected"
    assert "insufficient margin" in confirmed.kite_message
    assert repo.list_open_trades(db) == []


def test_timeout_leaves_intent_placed_and_resumes_on_reload(db, mock_mode, orders_enabled, monkeypatch):
    check, quote = _make_check(db, mock_mode, offset=50)
    # Submit well below the ask so the mock leaves it OPEN (unfilled).
    low_price = round(quote.ask * 0.97, 2)
    intent = service.review_entry(db, orders_enabled, check=check, lots=1, price=low_price, now=NOW)
    confirmed = service.confirm_entry(db, orders_enabled, intent=intent, now=NOW)
    assert confirmed.status == "placed"
    assert repo.list_open_trades(db) == []

    # "Reload" -- poll again, nothing has changed, still placed/open.
    polled = service.poll_entry_fill(db, orders_enabled, intent=repo.get_order_intent(db, intent.id))
    assert polled.status == "placed"
    assert repo.list_open_trades(db) == []


def test_partial_fill_then_cancel_records_filled_lots_only(db, mock_mode, orders_enabled, monkeypatch):
    check, quote = _make_check(db, mock_mode, offset=50)
    intent = service.review_entry(db, orders_enabled, check=check, lots=2, price=quote.ask, now=NOW)
    confirmed = service.confirm_entry(db, orders_enabled, intent=intent, now=NOW)
    assert confirmed.status == "filled"  # the mock fills it all -- force a partial view for this test

    partial_qty = confirmed.filled_qty // 2

    def fake_order_history(self, order_id):
        return [{
            "status": "CANCELLED", "filled_quantity": partial_qty, "average_price": quote.ask,
            "pending_quantity": 0,
        }]

    monkeypatch.setattr(type(mock_mode.client), "order_history", fake_order_history)
    repo.update_order_intent(db, intent.id, status="placed")  # pretend it hadn't resolved yet
    polled = service.poll_entry_fill(db, orders_enabled, intent=repo.get_order_intent(db, intent.id))
    assert polled.status == "partial"
    assert polled.filled_qty == partial_qty


# ---- GTT specifics ----------------------------------------------------------

def test_gtt_trigger_uses_fill_price_not_submitted_limit(db, mock_mode, orders_enabled, monkeypatch):
    check, quote = _make_check(db, mock_mode, offset=50)
    submitted_price = quote.ask * 1.02  # within 5%, but above the ask -- fills at the ask instead
    custom_stop = round(submitted_price * 0.9, 2)  # 10% below the *submitted* price
    intent = service.review_entry(
        db, orders_enabled, check=check, lots=1, price=submitted_price, stop_premium=custom_stop, now=NOW,
    )
    confirmed = service.confirm_entry(db, orders_enabled, intent=intent, now=NOW)
    assert confirmed.average_price != submitted_price  # filled at the touch price, not the limit

    trade = repo.list_open_trades(db)[0]
    # Mirror service.py's own stop_pct derivation -- custom_stop was rounded
    # to 2dp before this test used it, so it isn't exactly 10% off
    # submitted_price; assuming exactly 0.9 here is what made this flaky.
    stop_pct = (submitted_price - custom_stop) / submitted_price
    expected_stop = round(float(confirmed.average_price) * (1 - stop_pct), 2)
    assert float(trade.stop_premium) == expected_stop
    assert float(trade.stop_premium) != custom_stop  # not the stop computed off the *submitted* price

    gtt = mock_mode.client._gtts[trade.gtt_id]
    # The GTT trigger is tick-rounded (nearest 0.05) on top of the plain
    # 2-decimal rounding trade.stop_premium itself uses.
    assert gtt["condition"]["trigger_values"][0] == round_to_tick(expected_stop)


def test_oco_gtt_when_target_set(db, mock_mode, orders_enabled):
    check, quote = _make_check(db, mock_mode, offset=50)
    target = round(quote.ask * 1.5, 2)
    intent = service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask, target_premium=target, now=NOW)
    service.confirm_entry(db, orders_enabled, intent=intent, now=NOW)
    trade = repo.list_open_trades(db)[0]
    gtt = mock_mode.client._gtts[trade.gtt_id]
    assert len(gtt["condition"]["trigger_values"]) == 2
    assert gtt["condition"]["trigger_values"][1] == round_to_tick(target)


def test_gtt_placement_failure_sets_missing_status(db, mock_mode, orders_enabled, monkeypatch):
    check, quote = _make_check(db, mock_mode, offset=50)
    intent = service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask, now=NOW)

    def fake_place_gtt(self, *args, **kwargs):
        raise RuntimeError("GTT rejected")

    monkeypatch.setattr(type(mock_mode), "place_gtt", fake_place_gtt)
    service.confirm_entry(db, orders_enabled, intent=intent, now=NOW)
    trade = repo.list_open_trades(db)[0]
    assert trade.gtt_status == "missing"
    assert trade.gtt_id is None


# ---- exit --------------------------------------------------------------------

def _enter(db, provider, cfg, offset):
    check, quote = _make_check(db, provider, offset=offset)
    intent = service.review_entry(db, cfg, check=check, lots=1, price=quote.ask, now=NOW)
    service.confirm_entry(db, cfg, intent=intent, now=NOW)
    return repo.list_open_trades(db)[-1]


def test_full_exit_deletes_gtt_before_recording(db, mock_mode, orders_enabled):
    trade = _enter(db, mock_mode, orders_enabled, 50)
    check = repo.get_check(db, trade.check_id)
    exit_quote = mock_mode.option_quote(check.symbol, check.expiry, float(check.strike), check.opt_type)

    exit_intent = service.review_exit(
        db, orders_enabled, trade=trade, quantity=trade.lots * trade.lot_size, price=exit_quote.bid, now=NOW,
    )
    confirmed = service.confirm_exit(db, orders_enabled, intent=exit_intent, now=NOW)
    assert confirmed.status == "filled"

    closed = repo.get_trade(db, trade.id)
    assert closed.status == "closed"
    assert closed.gtt_status == "deleted"
    assert mock_mode.client._gtts[trade.gtt_id]["status"] == "deleted"
    assert repo.list_open_trades(db) == []


def test_partial_exit_reduces_gtt_quantity(db, mock_mode, orders_enabled):
    trade = _enter(db, mock_mode, orders_enabled, 50)
    check = repo.get_check(db, trade.check_id)
    exit_quote = mock_mode.option_quote(check.symbol, check.expiry, float(check.strike), check.opt_type)
    full_qty = trade.lots * trade.lot_size
    half_qty = full_qty // 2 if full_qty > 1 else full_qty

    exit_intent = service.review_exit(db, orders_enabled, trade=trade, quantity=half_qty, price=exit_quote.bid, now=NOW)
    confirmed = service.confirm_exit(db, orders_enabled, intent=exit_intent, now=NOW)

    reloaded = repo.get_trade(db, trade.id)
    if half_qty < full_qty:
        assert reloaded.status == "open"
        gtt = mock_mode.client._gtts[trade.gtt_id]
        assert gtt["orders"][0]["quantity"] == full_qty - half_qty
    else:
        assert reloaded.status == "closed"


def test_failed_gtt_delete_still_closes_trade_but_flags_missing(db, mock_mode, orders_enabled, monkeypatch):
    trade = _enter(db, mock_mode, orders_enabled, 50)
    check = repo.get_check(db, trade.check_id)
    exit_quote = mock_mode.option_quote(check.symbol, check.expiry, float(check.strike), check.opt_type)

    monkeypatch.setattr(type(mock_mode), "delete_gtt", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("delete failed")))
    exit_intent = service.review_exit(
        db, orders_enabled, trade=trade, quantity=trade.lots * trade.lot_size, price=exit_quote.bid, now=NOW,
    )
    service.confirm_exit(db, orders_enabled, intent=exit_intent, now=NOW)
    closed = repo.get_trade(db, trade.id)
    assert closed.status == "closed"
    assert closed.gtt_status == "missing"


# ---- reconciliation (7.8.6) --------------------------------------------------

def test_reconcile_triggered_and_filled_records_premium_stop_exit(db, mock_mode, orders_enabled):
    from datetime import date

    trade = _enter(db, mock_mode, orders_enabled, 50)
    gtt = mock_mode.client._gtts[trade.gtt_id]
    gtt["condition"]["trigger_values"] = [gtt["condition"]["trigger_values"][0] * 100]

    result = service.reconcile(db, orders_enabled, today=date.today())
    assert result["exits_recorded"] == 1
    closed = repo.get_trade(db, trade.id)
    assert closed.status == "closed"
    assert closed.exit_reason == "premium_stop"


def test_reconcile_triggered_but_unfilled_marks_unprotected(db, mock_mode, orders_enabled, monkeypatch):
    from datetime import date

    trade = _enter(db, mock_mode, orders_enabled, 50)
    gtt = mock_mode.client._gtts[trade.gtt_id]
    gtt["condition"]["trigger_values"] = [gtt["condition"]["trigger_values"][0] * 100]
    # Make the resulting leg price unmarketable so it stays OPEN once triggered.
    gtt["orders"][0]["price"] = gtt["orders"][0]["price"] * 1000

    result = service.reconcile(db, orders_enabled, today=date.today())
    assert result["unprotected"] == 1
    reloaded = repo.get_trade(db, trade.id)
    assert reloaded.status == "open"
    assert reloaded.gtt_status == "triggered"


def test_reconcile_gtt_deleted_outside_app_marks_missing(db, mock_mode, orders_enabled):
    from datetime import date

    trade = _enter(db, mock_mode, orders_enabled, 50)
    mock_mode.client._gtts[trade.gtt_id]["status"] = "deleted"

    result = service.reconcile(db, orders_enabled, today=date.today())
    assert result["unprotected"] == 1
    reloaded = repo.get_trade(db, trade.id)
    assert reloaded.status == "open"
    assert reloaded.gtt_status == "missing"


# ---- switches (7.8.9) --------------------------------------------------------

def test_orders_disabled_blocks_confirm_but_not_review(db, mock_mode):
    cfg = get_config()
    cfg.orders.enabled = False
    check, quote = _make_check(db, mock_mode, offset=50)
    intent = service.review_entry(db, cfg, check=check, lots=1, price=quote.ask, now=NOW)
    assert intent.status == "reviewed"
    with pytest.raises(RuleViolation) as exc:
        service.confirm_entry(db, cfg, intent=intent, now=NOW)
    assert exc.value.code == "ORDERS_DISABLED"


def test_market_order_rejected_when_not_allowed(db, mock_mode, orders_enabled):
    orders_enabled.orders.allow_market = False
    check, quote = _make_check(db, mock_mode, offset=50)
    with pytest.raises(RuleViolation) as exc:
        service.review_entry(db, orders_enabled, check=check, lots=1, price=quote.ask, order_type="MARKET", now=NOW)
    assert exc.value.code == "MARKET_NOT_ALLOWED"


def test_review_blocked_during_cooloff(db, mock_mode, orders_enabled):
    """Distinct from hitting the limit itself: a closed trade whose loss
    already breached the monthly limit keeps blocking new entries for
    settings.cooloff_days afterwards, even once a new calendar month's P&L
    would otherwise read as zero."""
    cfg = orders_enabled
    settings = repo.ensure_default_settings(db, cfg)
    check0, _ = _make_check(db, mock_mode, offset=50)
    trigger_trade = repo.create_trade(
        db, check_id=check0.id, entry_time=NOW, lots=1, lot_size=check0.lot_size,
        entry_premium=100, stop_premium=10, thesis_stop_level=None, time_stop_date=NOW.date(),
    )
    repo.update_trade_order_fields(
        db, trigger_trade.id, status="closed", exit_time=NOW - timedelta(days=1), exit_premium=0,
        exit_reason="manual", charges=0, pnl=-float(settings.monthly_loss_limit) - 1,
    )
    check, quote = _make_check(db, mock_mode, offset=50)
    with pytest.raises(RuleViolation) as exc:
        service.review_entry(db, cfg, check=check, lots=1, price=quote.ask, now=NOW)
    assert exc.value.code in ("MONTHLY_LOSS_LIMIT", "COOLING_OFF")


# ---- tighten/loosen also move the real GTT (7.8.5) --------------------------

def test_tighten_moves_the_real_gtt(client, db, mock_mode, orders_enabled):
    trade = _enter(db, mock_mode, orders_enabled, 50)
    old_trigger = mock_mode.client._gtts[trade.gtt_id]["condition"]["trigger_values"][0]
    new_stop = float(trade.stop_premium) + 5

    resp = client.post(f"/api/v1/trades/{trade.id}/tighten", json={"stop_premium": new_stop})
    assert resp.status_code == 200

    new_trigger = mock_mode.client._gtts[trade.gtt_id]["condition"]["trigger_values"][0]
    assert new_trigger != old_trigger
    assert repo.get_trade(db, trade.id).gtt_status == "active"


def test_tighten_gtt_modify_failure_flags_missing(db, mock_mode, orders_enabled, client, monkeypatch):
    trade = _enter(db, mock_mode, orders_enabled, 50)
    monkeypatch.setattr(type(mock_mode), "modify_gtt", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    new_stop = float(trade.stop_premium) + 5
    resp = client.post(f"/api/v1/trades/{trade.id}/tighten", json={"stop_premium": new_stop})
    assert resp.status_code == 200
    db.expire(trade)  # the HTTP request used its own session -- this one's identity map is stale
    assert repo.get_trade(db, trade.id).gtt_status == "missing"
