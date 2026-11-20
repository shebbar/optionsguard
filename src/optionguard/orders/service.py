"""Section 7.8.4/7.8.6: entry/exit review+confirm, fill tracking and
reconciliation. All guardrail decisions are delegated to rules/guardrails.py
(DESIGN.md principle 1) -- nothing here re-implements a check that already
exists there.

"Polling" (7.8.4 step 3) has no server-side background loop: the web UI
polls a thin status endpoint on an interval via HTMX, and each hit runs
poll_entry_fill()/poll_exit_fill() once. That's enough to satisfy
fill_poll_seconds without any new infrastructure, and matches how the rest
of this app computes everything on demand per request (e.g. Positions'
live P&L) rather than via a background job."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from optionguard.config import Config
from optionguard.db import repo
from optionguard.db.models import Check, OrderIntent, Trade
from optionguard.market.factory import apply_kite_session, get_kite_provider, get_mock_kite_provider
from optionguard.orders.gtt import build_gtt
from optionguard.pricing.calendar import trading_day_before
from optionguard.rules import guardrails
from optionguard.rules.errors import RuleViolation

INTENT_REVIEW_TTL = timedelta(minutes=2)


def provider_for(db: Session, data_source: str):
    if data_source == "mock":
        return get_mock_kite_provider()
    apply_kite_session(db)
    return get_kite_provider()


def is_ip_error_message(message: str | None) -> bool:
    """Heuristic for Kite's static-IP rejection (7.8.2) -- verify the exact
    wording against current Zerodha docs before go-live; this is
    deliberately loose rather than matching one hardcoded string."""
    if not message:
        return False
    lowered = message.lower()
    return "ip" in lowered and any(w in lowered for w in ("whitelist", "not valid", "unauthorized", "static"))


def _contract_key(symbol: str, expiry: date, strike: float, opt_type: str) -> str:
    """A per-contract key for our own order_intents dedup column -- not a
    real Kite tradingsymbol (section 5's "resolve through the instruments
    dump, never construct by string formatting" is about what's sent *to*
    Kite; KiteProvider.place_order/place_gtt still resolve the real
    tradingsymbol themselves, from symbol/expiry/strike/opt_type)."""
    return f"{symbol.upper()}|{expiry.isoformat()}|{strike:g}|{opt_type.upper()}"


def guard_orders_enabled(cfg: Config) -> None:
    if not cfg.orders.enabled:
        raise RuleViolation(
            "ORDERS_DISABLED", "Order placement is disabled ([orders].enabled = false) -- preview only.",
            overridable=False,
        )


def guard_order_type_allowed(cfg: Config, order_type: str) -> None:
    if order_type == "MARKET" and not cfg.orders.allow_market:
        raise RuleViolation("MARKET_NOT_ALLOWED", "MARKET orders are disabled -- use LIMIT.", overridable=False)


def _live_intent_or_expire(db: Session, key: str, now: datetime) -> OrderIntent | None:
    """A 'reviewed' intent that was never confirmed within INTENT_REVIEW_TTL
    is abandoned, not live -- lazily expire it here (rather than a
    background job, matching this app's on-demand-computation style) so it
    stops blocking new reviews on the same contract forever."""
    intent = repo.get_live_intent_for_contract(db, key)
    if intent is not None and intent.status == "reviewed" and now - intent.created_at > INTENT_REVIEW_TTL:
        return repo.update_order_intent(db, intent.id, status="expired")
    return intent


# ---- entry ----------------------------------------------------------------

def review_entry(
    db: Session, cfg: Config, *, check: Check, lots: int, price: float, order_type: str = "LIMIT",
    stop_premium: float | None = None, target_premium: float | None = None,
    override_reason: str | None = None, now: datetime, thesis_id: int | None = None,
) -> OrderIntent:
    holidays = set(cfg.holidays.dates)
    settings = repo.ensure_default_settings(db, cfg)
    guard_order_type_allowed(cfg, order_type)
    guardrails.guard_record_trade(db, check, lots, settings, now, holidays, override_reason)

    key = _contract_key(check.symbol, check.expiry, float(check.strike), check.opt_type)
    guardrails.guard_no_live_order_intent(_live_intent_or_expire(db, key, now))
    existing_trade = repo.get_open_trade_for_contract(db, check.symbol, check.expiry, float(check.strike), check.opt_type)
    if existing_trade is not None:
        raise RuleViolation(
            "TRADE_ALREADY_OPEN", f"An open trade already exists for this contract (trade id={existing_trade.id}).",
            overridable=False,
        )

    thesis = None
    if thesis_id is not None:
        thesis = repo.get_option_thesis(db, thesis_id)
        if thesis is None:
            raise RuleViolation("THESIS_NOT_FOUND", f"option thesis {thesis_id} not found.", overridable=False)
        if thesis.mode != "real" or thesis.status != "open":
            raise RuleViolation(
                "THESIS_NOT_OPEN", "Only an open, real option thesis can be linked to an order.", overridable=False,
            )
        if thesis.trade_id is not None:
            raise RuleViolation(
                "THESIS_ALREADY_LINKED", f"Thesis {thesis_id} is already linked to trade {thesis.trade_id}.",
                overridable=False,
            )
        if (
            thesis.symbol != check.symbol or thesis.expiry != check.expiry
            or float(thesis.strike) != float(check.strike) or thesis.opt_type != check.opt_type
        ):
            raise RuleViolation(
                "THESIS_CONTRACT_MISMATCH", "The thesis's contract doesn't match this check's contract.",
                overridable=False,
            )

    provider = provider_for(db, check.data_source)
    quote = provider.option_quote(check.symbol, check.expiry, float(check.strike), check.opt_type)
    live_reference = quote.ask if quote.ask is not None else quote.last_price
    if order_type == "LIMIT":
        guardrails.guard_order_price(price, live_reference)

    if stop_premium is None:
        stop_premium = float(thesis.stop_premium) if thesis is not None else round(price * (1 - float(settings.premium_stop_pct)), 2)
    else:
        guardrails.guard_stop_no_looser_than_settings(stop_premium, price, settings)
    if target_premium is None and thesis is not None:
        target_premium = float(thesis.target_premium)  # 7.9.10: always OCO when linked to a thesis

    if thesis is not None:
        guardrails.guard_thesis_tighten_only(thesis, lots=lots, stop_premium=stop_premium, target_premium=target_premium)

    return repo.create_order_intent(
        db, kind="entry", check_id=check.id, tradingsymbol=key, quantity=lots * check.lot_size,
        order_type=order_type, price=price, stop_premium=stop_premium, target_premium=target_premium,
        override_reason=override_reason, option_thesis_id=thesis_id,
    )


def confirm_entry(db: Session, cfg: Config, *, intent: OrderIntent, now: datetime) -> OrderIntent:
    guard_orders_enabled(cfg)
    if intent.status != "reviewed":
        return intent  # already placed (or further along) -- a double-submit is a no-op

    guardrails.guard_intent_not_expired(intent, now, INTENT_REVIEW_TTL)
    check = repo.get_check(db, intent.check_id)
    settings = repo.ensure_default_settings(db, cfg)
    holidays = set(cfg.holidays.dates)
    # Re-run the same guardrails -- state may have changed since Review.
    guardrails.guard_record_trade(db, check, intent.quantity // check.lot_size, settings, now, holidays, intent.override_reason)

    placed = repo.try_transition_order_intent(db, intent.id, from_status="reviewed", to_status="placed")
    if placed is None:
        return repo.get_order_intent(db, intent.id)  # lost the idempotency race -- not a new order

    provider = provider_for(db, check.data_source)
    try:
        order_id = provider.place_order(
            check.symbol, check.expiry, float(check.strike), check.opt_type, "BUY", placed.quantity,
            placed.order_type, price=placed.price, tag=f"OG{placed.id}",
        )
    except Exception as exc:  # noqa: BLE001 -- shown verbatim, see module docstring
        return repo.update_order_intent(db, placed.id, status="rejected", kite_message=str(exc))

    updated = repo.update_order_intent(db, placed.id, order_id=order_id)
    return poll_entry_fill(db, cfg, intent=updated)


def poll_entry_fill(db: Session, cfg: Config, *, intent: OrderIntent) -> OrderIntent:
    if intent.status != "placed" or not intent.order_id:
        return intent
    check = repo.get_check(db, intent.check_id)
    provider = provider_for(db, check.data_source)
    try:
        latest = provider.order_history(intent.order_id)[-1]
    except Exception as exc:  # noqa: BLE001
        return repo.update_order_intent(db, intent.id, kite_message=str(exc))

    filled_qty = int(latest.get("filled_quantity") or 0)
    average_price = latest.get("average_price")
    status = latest.get("status")

    if status == "COMPLETE":
        record_fill_and_protect(db, cfg, intent=intent, filled_qty=filled_qty, average_price=float(average_price))
        return repo.update_order_intent(db, intent.id, status="filled", filled_qty=filled_qty, average_price=average_price)
    if status == "CANCELLED" and filled_qty > 0:
        record_fill_and_protect(db, cfg, intent=intent, filled_qty=filled_qty, average_price=float(average_price))
        return repo.update_order_intent(db, intent.id, status="partial", filled_qty=filled_qty, average_price=average_price)
    if status in ("CANCELLED", "REJECTED") and filled_qty == 0:
        return repo.update_order_intent(
            db, intent.id, status="cancelled" if status == "CANCELLED" else "rejected",
            kite_message=latest.get("status_message"),
        )
    return intent  # still OPEN -- left as "placed" for the next poll/page load


def record_fill_and_protect(db: Session, cfg: Config, *, intent: OrderIntent, filled_qty: int, average_price: float) -> Trade:
    """7.8.4 step 4: record the trade at the *actual fill price*, then
    place its GTT. A GTT failure doesn't block recording the trade --
    gtt_status='missing' drives the same red-banner warning has_gtt already
    does elsewhere (Positions)."""
    check = repo.get_check(db, intent.check_id)
    settings = repo.ensure_default_settings(db, cfg)
    holidays = set(cfg.holidays.dates)

    stop_pct = (float(intent.price) - float(intent.stop_premium)) / float(intent.price)
    final_stop = round(average_price * (1 - stop_pct), 2)
    lots = filled_qty // check.lot_size

    trade = repo.create_trade(
        db, check_id=check.id, entry_time=repo.utcnow(), lots=lots, lot_size=check.lot_size,
        entry_premium=average_price, stop_premium=final_stop, thesis_stop_level=None,
        time_stop_date=trading_day_before(check.expiry, settings.time_stop_days, holidays),
        entry_intent_id=intent.id,
    )

    provider = provider_for(db, check.data_source)
    gtt_spec = build_gtt(
        quantity=filled_qty, stop_trigger=final_stop, buffer_pct=cfg.orders.gtt_limit_buffer_pct,
        target_trigger=float(intent.target_premium) if intent.target_premium is not None else None,
    )
    try:
        gtt_id = provider.place_gtt(
            check.symbol, check.expiry, float(check.strike), check.opt_type,
            gtt_spec["trigger_values"], gtt_spec["orders"],
        )
        repo.update_trade_order_fields(db, trade.id, gtt_id=gtt_id, gtt_status="active")
    except Exception:  # noqa: BLE001 -- the red banner (has_gtt-style) carries the warning, not an exception
        repo.update_trade_order_fields(db, trade.id, gtt_status="missing")

    if intent.option_thesis_id is not None:
        # 7.9.10: link the thesis to its trade and update entry_premium to
        # the fill, in one call -- both fields are each settable only once,
        # and only together like this (entry_premium stops being mutable
        # the moment trade_id is set).
        repo.update_option_thesis(db, intent.option_thesis_id, trade_id=trade.id, entry_premium=average_price)

    db.refresh(trade)
    return trade


def retry_gtt(db: Session, cfg: Config, *, trade: Trade) -> Trade:
    """7.8.4 step 4 / the Retry GTT button: (re)place a GTT for a trade
    that currently has none."""
    check = repo.get_check(db, trade.check_id)
    provider = provider_for(db, check.data_source)
    gtt_spec = build_gtt(
        quantity=trade.lots * trade.lot_size, stop_trigger=float(trade.stop_premium),
        buffer_pct=cfg.orders.gtt_limit_buffer_pct,
    )
    gtt_id = provider.place_gtt(
        check.symbol, check.expiry, float(check.strike), check.opt_type,
        gtt_spec["trigger_values"], gtt_spec["orders"],
    )
    return repo.update_trade_order_fields(db, trade.id, gtt_id=gtt_id, gtt_status="active")


def sync_gtt_after_stop_change(db: Session, cfg: Config, *, trade: Trade) -> Trade:
    """7.8.5: tighten and loosen (existing /api/v1 and /ui routes) both call
    modify_gtt after changing trade.stop_premium, so the real GTT at Kite
    never silently diverges from what this app records -- the one Kite-side
    effect allowed from /api/v1, since tighten can only reduce risk. Call
    *after* trade.stop_premium is already committed to its new value.
    No-op if the trade has no active GTT (nothing to keep in sync); any
    original OCO target is dropped on rebuild -- Trade has no target_premium
    column to remember it by (only order_intents does, transiently)."""
    if not trade.gtt_id or trade.gtt_status != "active":
        return trade
    check = repo.get_check(db, trade.check_id)
    provider = provider_for(db, check.data_source)
    gtt_spec = build_gtt(
        quantity=trade.lots * trade.lot_size, stop_trigger=float(trade.stop_premium),
        buffer_pct=cfg.orders.gtt_limit_buffer_pct,
    )
    try:
        provider.modify_gtt(
            trade.gtt_id, check.symbol, check.expiry, float(check.strike), check.opt_type,
            gtt_spec["trigger_values"], gtt_spec["orders"],
        )
        return trade
    except Exception:  # noqa: BLE001 -- surfaced via gtt_status, not raised
        return repo.update_trade_order_fields(db, trade.id, gtt_status="missing")


# ---- exit -------------------------------------------------------------------

def review_exit(
    db: Session, cfg: Config, *, trade: Trade, quantity: int, price: float, order_type: str = "LIMIT",
    exit_reason: str | None = None, now: datetime,
) -> OrderIntent:
    guard_order_type_allowed(cfg, order_type)
    guardrails.guard_exit_quantity(quantity, trade)
    check = repo.get_check(db, trade.check_id)

    key = _contract_key(check.symbol, check.expiry, float(check.strike), check.opt_type)
    guardrails.guard_no_live_order_intent(_live_intent_or_expire(db, key, now))

    provider = provider_for(db, check.data_source)
    quote = provider.option_quote(check.symbol, check.expiry, float(check.strike), check.opt_type)
    live_reference = quote.bid if quote.bid is not None else quote.last_price
    if order_type == "LIMIT":
        guardrails.guard_order_price(price, live_reference)

    return repo.create_order_intent(
        db, kind="exit", trade_id=trade.id, tradingsymbol=key, quantity=quantity,
        order_type=order_type, price=price, override_reason=exit_reason,
    )


def confirm_exit(db: Session, cfg: Config, *, intent: OrderIntent, now: datetime) -> OrderIntent:
    guard_orders_enabled(cfg)
    if intent.status != "reviewed":
        return intent

    guardrails.guard_intent_not_expired(intent, now, INTENT_REVIEW_TTL)
    trade = repo.get_trade(db, intent.trade_id)
    guardrails.guard_exit_quantity(intent.quantity, trade)
    check = repo.get_check(db, trade.check_id)

    placed = repo.try_transition_order_intent(db, intent.id, from_status="reviewed", to_status="placed")
    if placed is None:
        return repo.get_order_intent(db, intent.id)

    provider = provider_for(db, check.data_source)
    try:
        order_id = provider.place_order(
            check.symbol, check.expiry, float(check.strike), check.opt_type, "SELL", placed.quantity,
            placed.order_type, price=placed.price, tag=f"OG{placed.id}",
        )
    except Exception as exc:  # noqa: BLE001
        return repo.update_order_intent(db, placed.id, status="rejected", kite_message=str(exc))

    updated = repo.update_order_intent(db, placed.id, order_id=order_id)
    return poll_exit_fill(db, cfg, intent=updated)


def poll_exit_fill(db: Session, cfg: Config, *, intent: OrderIntent) -> OrderIntent:
    if intent.status != "placed" or not intent.order_id:
        return intent
    trade = repo.get_trade(db, intent.trade_id)
    check = repo.get_check(db, trade.check_id)
    provider = provider_for(db, check.data_source)
    try:
        latest = provider.order_history(intent.order_id)[-1]
    except Exception as exc:  # noqa: BLE001
        return repo.update_order_intent(db, intent.id, kite_message=str(exc))

    filled_qty = int(latest.get("filled_quantity") or 0)
    average_price = latest.get("average_price")
    status = latest.get("status")

    if status == "COMPLETE":
        record_exit_fill(db, cfg, intent=intent, trade=trade, filled_qty=filled_qty, average_price=float(average_price))
        return repo.update_order_intent(db, intent.id, status="filled", filled_qty=filled_qty, average_price=average_price)
    if status == "CANCELLED" and filled_qty > 0:
        record_exit_fill(db, cfg, intent=intent, trade=trade, filled_qty=filled_qty, average_price=float(average_price))
        return repo.update_order_intent(db, intent.id, status="partial", filled_qty=filled_qty, average_price=average_price)
    if status in ("CANCELLED", "REJECTED") and filled_qty == 0:
        return repo.update_order_intent(
            db, intent.id, status="cancelled" if status == "CANCELLED" else "rejected",
            kite_message=latest.get("status_message"),
        )
    return intent


def record_exit_fill(
    db: Session, cfg: Config, *, intent: OrderIntent, trade: Trade, filled_qty: int, average_price: float,
    skip_gtt_dance: bool = False,
) -> Trade:
    """7.8.6: delete the GTT *before* recording the exit -- a leftover sell
    GTT on a closed position would try to sell a contract no longer held.
    A partial exit instead modifies the GTT down to the remaining quantity.
    skip_gtt_dance=True is for reconcile()'s own triggered-and-filled path --
    the GTT already fired and consumed itself; there's nothing left to
    delete or modify."""
    check = repo.get_check(db, trade.check_id)
    provider = provider_for(db, check.data_source)
    open_qty = trade.lots * trade.lot_size
    full_exit = filled_qty >= open_qty

    gtt_delete_failed = False
    if not skip_gtt_dance and trade.gtt_id and trade.gtt_status == "active":
        try:
            if full_exit:
                provider.delete_gtt(trade.gtt_id)
            else:
                remaining = open_qty - filled_qty
                gtt_spec = build_gtt(
                    quantity=remaining, stop_trigger=float(trade.stop_premium),
                    buffer_pct=cfg.orders.gtt_limit_buffer_pct,
                )
                provider.modify_gtt(
                    trade.gtt_id, check.symbol, check.expiry, float(check.strike), check.opt_type,
                    gtt_spec["trigger_values"], gtt_spec["orders"],
                )
        except Exception:  # noqa: BLE001 -- surfaced via gtt_status, not raised
            gtt_delete_failed = True

    if full_exit:
        charges = cfg.defaults.charges_per_order_estimate
        pnl = (average_price - float(trade.entry_premium)) * trade.lots * trade.lot_size - float(charges)
        repo.update_trade_order_fields(
            db, trade.id, status="closed", exit_time=repo.utcnow(), exit_premium=average_price,
            exit_reason=intent.override_reason if intent.override_reason in (
                "premium_stop", "thesis_stop", "time_stop", "target", "manual", "expiry",
            ) else "manual",
            charges=charges, pnl=pnl, exit_intent_id=intent.id,
            gtt_status="deleted" if not gtt_delete_failed else "missing",
        )
    else:
        new_lots = (open_qty - filled_qty) // trade.lot_size
        repo.update_trade_order_fields(
            db, trade.id, lots=new_lots, exit_intent_id=intent.id,
            gtt_status=trade.gtt_status if not gtt_delete_failed else "missing",
        )
    db.refresh(trade)
    return trade


# ---- reconciliation (7.8.6, run as sync step 0 -- see theses/sync.py) ------

def reconcile(db: Session, cfg: Config, *, today: date) -> dict:
    exits_recorded = 0
    unprotected = 0
    errors: list[dict] = []

    for trade in repo.list_open_trades(db):
        if not trade.gtt_id or trade.gtt_status != "active":
            continue
        check = repo.get_check(db, trade.check_id)
        provider = provider_for(db, check.data_source)
        try:
            gtt = provider.get_gtt(trade.gtt_id)
        except Exception as exc:  # noqa: BLE001
            errors.append({"trade_id": trade.id, "step": "reconcile_gtt", "message": str(exc)})
            continue

        status = gtt.get("status")
        if status == "active":
            continue
        if status == "triggered":
            legs = gtt.get("orders", [])
            triggered_idx = next((i for i, o in enumerate(legs) if o.get("result")), None)
            result = legs[triggered_idx].get("result") if triggered_idx is not None else None
            if result and result.get("status") == "COMPLETE":
                # build_gtt() always puts the target leg last, when present.
                exit_reason = "target" if (triggered_idx == len(legs) - 1 and len(legs) > 1) else "premium_stop"
                fake_intent = OrderIntent(override_reason=exit_reason, target_premium=None)
                record_exit_fill(
                    db, cfg, intent=fake_intent, trade=trade,
                    filled_qty=int(result.get("filled_quantity") or trade.lots * trade.lot_size),
                    average_price=float(result.get("average_price")),
                    skip_gtt_dance=True,
                )
                exits_recorded += 1
            else:
                repo.update_trade_order_fields(db, trade.id, gtt_status="triggered")
                unprotected += 1
        else:  # cancelled / deleted / expired outside the app
            repo.update_trade_order_fields(db, trade.id, gtt_status="missing")
            unprotected += 1

    resumed = 0
    for intent in repo.list_placed_order_intents(db):
        try:
            if intent.kind == "entry":
                poll_entry_fill(db, cfg, intent=intent)
            else:
                poll_exit_fill(db, cfg, intent=intent)
            resumed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"intent_id": intent.id, "step": "resume_poll", "message": str(exc)})

    return {"exits_recorded": exits_recorded, "unprotected": unprotected, "intents_resumed": resumed, "errors": errors}
