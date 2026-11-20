"""POST /ui/order/review, POST /ui/order/confirm, GET /ui/order/{id}/status,
POST /ui/order/{id}/modify, POST /ui/order/{id}/cancel, POST
/ui/trades/{id}/gtt, GET /api/v1/orders/intents. Per DESIGN_7_8_order_ticket.md
7.8.8.

Isolation (7.8.1's non-goal): everything that can place, modify or cancel an
order or a GTT lives only under /ui, CSRF-protected -- never under /api/v1.
MCP only ever talks to /api/v1, so it can never reach any of this. The one
/api/v1 route here (list intents) is read-only."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from optionguard.api.errors import ApiError
from optionguard.api.schemas import OrderConfirmIn, OrderModifyIn, OrderReviewIn
from optionguard.api.serializers import order_intent_to_dict, trade_to_dict
from optionguard.config import get_config
from optionguard.db import repo
from optionguard.db.models import OrderIntent, Trade
from optionguard.db.session import get_db
from optionguard.orders import service
from optionguard.rules.errors import RuleViolation
from optionguard.web.csrf import verify_csrf

router = APIRouter(prefix="/api/v1", tags=["orders"])
ui_router = APIRouter(prefix="/ui", tags=["orders-ui"], dependencies=[Depends(verify_csrf)])


def _require_check(db: Session, check_id: int):
    check = repo.get_check(db, check_id)
    if check is None:
        raise ApiError("NOT_FOUND", f"check {check_id} not found", status_code=404)
    return check


def _require_trade(db: Session, trade_id: int) -> Trade:
    trade = repo.get_trade(db, trade_id)
    if trade is None:
        raise ApiError("NOT_FOUND", f"trade {trade_id} not found", status_code=404)
    return trade


def _require_intent(db: Session, intent_id: int) -> OrderIntent:
    intent = repo.get_order_intent(db, intent_id)
    if intent is None:
        raise ApiError("NOT_FOUND", f"order intent {intent_id} not found", status_code=404)
    return intent


def _check_for_intent(db: Session, intent: OrderIntent):
    if intent.kind == "entry":
        return repo.get_check(db, intent.check_id)
    return repo.get_check(db, repo.get_trade(db, intent.trade_id).check_id)


@ui_router.post("/order/review")
def post_order_review(body: OrderReviewIn, db: Session = Depends(get_db)):
    cfg = get_config()
    now = datetime.now(timezone.utc)
    try:
        if body.kind == "entry":
            if body.check_id is None or body.lots is None:
                raise ApiError("MISSING_FIELDS", "check_id and lots are required for an entry review.", status_code=422)
            check = _require_check(db, body.check_id)
            intent = service.review_entry(
                db, cfg, check=check, lots=body.lots, price=body.price, order_type=body.order_type,
                stop_premium=body.stop_premium, target_premium=body.target_premium,
                override_reason=body.override_reason, now=now, thesis_id=body.thesis_id,
            )
        else:
            if body.trade_id is None or body.quantity is None:
                raise ApiError("MISSING_FIELDS", "trade_id and quantity are required for an exit review.", status_code=422)
            trade = _require_trade(db, body.trade_id)
            intent = service.review_exit(
                db, cfg, trade=trade, quantity=body.quantity, price=body.price, order_type=body.order_type,
                exit_reason=body.override_reason, now=now,
            )
    except RuleViolation as exc:
        raise ApiError(exc.code, exc.message, status_code=409, overridable=exc.overridable) from exc
    return order_intent_to_dict(intent)


@ui_router.post("/order/confirm")
def post_order_confirm(body: OrderConfirmIn, db: Session = Depends(get_db)):
    cfg = get_config()
    intent = _require_intent(db, body.intent_id)
    now = datetime.now(timezone.utc)
    try:
        confirmed = (
            service.confirm_entry(db, cfg, intent=intent, now=now) if intent.kind == "entry"
            else service.confirm_exit(db, cfg, intent=intent, now=now)
        )
    except RuleViolation as exc:
        raise ApiError(exc.code, exc.message, status_code=409, overridable=exc.overridable) from exc
    return order_intent_to_dict(confirmed)


@ui_router.get("/order/{intent_id}/status")
def get_order_status(intent_id: int, db: Session = Depends(get_db)):
    cfg = get_config()
    intent = _require_intent(db, intent_id)
    polled = (
        service.poll_entry_fill(db, cfg, intent=intent) if intent.kind == "entry"
        else service.poll_exit_fill(db, cfg, intent=intent)
    )
    return order_intent_to_dict(polled)


@ui_router.post("/order/{intent_id}/modify")
def post_order_modify(intent_id: int, body: OrderModifyIn, db: Session = Depends(get_db)):
    cfg = get_config()
    intent = _require_intent(db, intent_id)
    if intent.status != "placed" or not intent.order_id:
        raise ApiError("NOT_MODIFIABLE", "Only a placed, unfilled order can be modified.", status_code=409)

    check = _check_for_intent(db, intent)
    provider = service.provider_for(db, check.data_source)
    try:
        provider.modify_order(intent.order_id, price=body.price, quantity=body.quantity)
    except Exception as exc:  # noqa: BLE001 -- shown verbatim
        raise ApiError("KITE_ERROR", str(exc), status_code=409) from exc

    if body.price is not None:
        repo.update_order_intent(db, intent.id, price=body.price)
    updated = repo.get_order_intent(db, intent.id)
    polled = (
        service.poll_entry_fill(db, cfg, intent=updated) if intent.kind == "entry"
        else service.poll_exit_fill(db, cfg, intent=updated)
    )
    return order_intent_to_dict(polled)


@ui_router.post("/order/{intent_id}/cancel")
def post_order_cancel(intent_id: int, db: Session = Depends(get_db)):
    intent = _require_intent(db, intent_id)
    if intent.status != "placed" or not intent.order_id:
        raise ApiError("NOT_CANCELLABLE", "Only a placed, unfilled order can be cancelled.", status_code=409)

    check = _check_for_intent(db, intent)
    provider = service.provider_for(db, check.data_source)
    try:
        provider.cancel_order(intent.order_id)
    except Exception as exc:  # noqa: BLE001 -- shown verbatim
        raise ApiError("KITE_ERROR", str(exc), status_code=409) from exc
    updated = repo.update_order_intent(db, intent.id, status="cancelled")
    return order_intent_to_dict(updated)


@ui_router.post("/trades/{trade_id}/gtt")
def post_retry_gtt(trade_id: int, db: Session = Depends(get_db)):
    cfg = get_config()
    trade = _require_trade(db, trade_id)
    try:
        updated = service.retry_gtt(db, cfg, trade=trade)
    except Exception as exc:  # noqa: BLE001 -- shown verbatim
        raise ApiError("KITE_ERROR", str(exc), status_code=409) from exc
    return trade_to_dict(updated)


@router.get("/orders/intents")
def get_order_intents(status: str | None = None, db: Session = Depends(get_db)):
    """Read-only -- MCP may call this (7.8.8)."""
    return [order_intent_to_dict(i) for i in repo.list_order_intents(db, status=status)]
