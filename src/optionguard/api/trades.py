"""POST /trades, POST /trades/{id}/tighten, POST /trades/{id}/exit, GET
/positions. Per DESIGN.md section 11. GET /positions computes live P&L on
demand (no background poller -- alerts/monitor loop are descoped from v1)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from optionguard.api.errors import ApiError
from optionguard.api.schemas import ExitIn, LoosenIn, TightenIn, TradeIn
from optionguard.api.serializers import trade_to_dict
from optionguard.config import get_config
from optionguard.db import repo
from optionguard.db.models import Trade
from optionguard.db.session import get_db
from optionguard.market.base import NotAvailable
from optionguard.market.factory import apply_kite_session, get_kite_provider, get_mock_kite_provider
from optionguard.orders.service import sync_gtt_after_stop_change
from optionguard.pricing.calendar import trading_day_before, trading_days_to_expiry
from optionguard.rules import guardrails
from optionguard.rules.errors import RuleViolation
from optionguard.web.csrf import verify_csrf

router = APIRouter(prefix="/api/v1", tags=["trades"])
ui_router = APIRouter(prefix="/ui", tags=["trades-ui"], dependencies=[Depends(verify_csrf)])


def _require_trade(db: Session, trade_id: int) -> Trade:
    trade = repo.get_trade(db, trade_id)
    if trade is None:
        raise ApiError("NOT_FOUND", f"trade {trade_id} not found", status_code=404)
    return trade


@router.post("/trades")
def post_trade(body: TradeIn, db: Session = Depends(get_db)):
    cfg = get_config()
    holidays = set(cfg.holidays.dates)
    settings = repo.ensure_default_settings(db, cfg)
    check = repo.get_check(db, body.check_id)
    if check is None:
        raise ApiError("NOT_FOUND", f"check {body.check_id} not found", status_code=404)

    now = datetime.now(timezone.utc)
    try:
        guardrails.guard_record_trade(db, check, body.lots, settings, now, holidays, body.override_reason)
    except RuleViolation as exc:
        raise ApiError(exc.code, exc.message, status_code=409, overridable=exc.overridable) from exc

    stop_premium = body.stop_premium
    if stop_premium is None:
        stop_premium = float(body.entry_premium) * (1 - float(settings.premium_stop_pct))
    time_stop_date = body.time_stop_date
    if time_stop_date is None:
        time_stop_date = trading_day_before(check.expiry, settings.time_stop_days, holidays)

    trade = repo.create_trade(
        db,
        check_id=check.id, entry_time=now, lots=body.lots, lot_size=check.lot_size,
        entry_premium=body.entry_premium, stop_premium=stop_premium,
        thesis_stop_level=body.thesis_stop_level, time_stop_date=time_stop_date,
    )

    if check.verdict == "red" and body.override_reason:
        repo.add_override(db, check_id=check.id, trade_id=trade.id, rule="RED_VERDICT", reason=body.override_reason)

    return trade_to_dict(trade)


@router.post("/trades/{trade_id}/tighten")
def post_tighten(trade_id: int, body: TightenIn, db: Session = Depends(get_db)):
    trade = _require_trade(db, trade_id)
    check = repo.get_check(db, trade.check_id)
    # Judged against the check's reference spot, not a live feed, so tighten
    # validation is deterministic for both manual and kite trades.
    reference_spot = float(check.spot) if check else None

    try:
        guardrails.guard_tighten_stop(
            old_stop_premium=float(trade.stop_premium),
            new_stop_premium=body.stop_premium,
            old_thesis_stop_level=float(trade.thesis_stop_level) if trade.thesis_stop_level is not None else None,
            new_thesis_stop_level=body.thesis_stop_level,
            old_time_stop_date=trade.time_stop_date,
            new_time_stop_date=body.time_stop_date,
            spot=reference_spot,
        )
    except RuleViolation as exc:
        raise ApiError(exc.code, exc.message, status_code=409, overridable=exc.overridable) from exc

    if body.stop_premium is not None:
        repo.add_stop_change(
            db, trade_id=trade.id, field="stop_premium",
            old_value=str(trade.stop_premium), new_value=str(body.stop_premium), direction="tighten",
        )
        trade.stop_premium = body.stop_premium
    if body.thesis_stop_level is not None:
        repo.add_stop_change(
            db, trade_id=trade.id, field="thesis_stop_level",
            old_value=str(trade.thesis_stop_level), new_value=str(body.thesis_stop_level), direction="tighten",
        )
        trade.thesis_stop_level = body.thesis_stop_level
    if body.time_stop_date is not None:
        repo.add_stop_change(
            db, trade_id=trade.id, field="time_stop_date",
            old_value=str(trade.time_stop_date), new_value=str(body.time_stop_date), direction="tighten",
        )
        trade.time_stop_date = body.time_stop_date

    db.commit()
    db.refresh(trade)
    if body.stop_premium is not None:
        # Section 7.8.5: the one Kite-side effect allowed from /api/v1 --
        # tighten can only reduce risk, so it's safe to let it move the
        # real GTT too.
        trade = sync_gtt_after_stop_change(db, get_config(), trade=trade)
    return trade_to_dict(trade)


@router.post("/trades/{trade_id}/exit")
def post_exit(trade_id: int, body: ExitIn, db: Session = Depends(get_db)):
    cfg = get_config()
    trade = _require_trade(db, trade_id)
    if trade.status == "closed":
        raise ApiError("ALREADY_CLOSED", f"trade {trade_id} is already closed")

    charges = body.charges if body.charges is not None else cfg.defaults.charges_per_order_estimate
    pnl = (float(body.exit_premium) - float(trade.entry_premium)) * trade.lots * trade.lot_size - float(charges)

    trade.status = "closed"
    trade.exit_time = datetime.now(timezone.utc)
    trade.exit_premium = body.exit_premium
    trade.exit_reason = body.exit_reason
    trade.charges = charges
    trade.pnl = pnl
    db.commit()
    db.refresh(trade)
    return trade_to_dict(trade)


@ui_router.post("/trades/{trade_id}/loosen")
def post_loosen(trade_id: int, body: LoosenIn, db: Session = Depends(get_db)):
    """UI-only. Not reachable via /api/v1, so the MCP server can't call it."""
    trade = _require_trade(db, trade_id)
    try:
        guardrails.guard_loosen_stop(body.reason)
    except RuleViolation as exc:
        raise ApiError(exc.code, exc.message, status_code=409) from exc

    changed = False
    for field, new_value in (
        ("stop_premium", body.stop_premium),
        ("thesis_stop_level", body.thesis_stop_level),
        ("time_stop_date", body.time_stop_date),
    ):
        if new_value is not None:
            repo.add_stop_change(
                db, trade_id=trade.id, field=field,
                old_value=str(getattr(trade, field)), new_value=str(new_value), direction="loosen",
            )
            setattr(trade, field, new_value)
            changed = True
    if not changed:
        raise ApiError("NO_CHANGE", "No stop field was provided to loosen.")

    repo.add_override(db, trade_id=trade.id, rule="LOOSEN_STOP", reason=body.reason)
    db.commit()
    db.refresh(trade)
    if body.stop_premium is not None:
        # Section 7.8.5: loosen also moves the real GTT -- it's logged via
        # the override above either way, same as loosening always was.
        trade = sync_gtt_after_stop_change(db, get_config(), trade=trade)
    return trade_to_dict(trade)


@router.get("/positions")
def get_positions(db: Session = Depends(get_db)):
    cfg = get_config()
    holidays = set(cfg.holidays.dates)
    today = date.today()
    out = []
    for trade in repo.list_open_trades(db):
        check = repo.get_check(db, trade.check_id)
        live_premium = live_spot = has_gtt = None
        if check and check.data_source in ("kite", "mock"):
            # A check tagged "mock" must keep using mock data even if real
            # credentials exist now -- its contract may not even be real.
            if check.data_source == "mock":
                provider = get_mock_kite_provider()
            else:
                apply_kite_session(db)
                provider = get_kite_provider()
            try:
                quote = provider.option_quote(check.symbol, check.expiry, float(check.strike), check.opt_type)
                live_premium = quote.last_price
                live_spot = provider.spot(check.symbol)
                has_gtt = provider.has_gtt(check.symbol, check.expiry, float(check.strike), check.opt_type)
            except NotAvailable:
                pass

        unrealized_pnl = None
        if live_premium is not None:
            unrealized_pnl = (live_premium - float(trade.entry_premium)) * trade.lots * trade.lot_size

        entry = trade_to_dict(trade)
        entry["symbol"] = check.symbol if check else None
        entry["expiry"] = check.expiry.isoformat() if check else None
        entry["strike"] = float(check.strike) if check else None
        entry["opt_type"] = check.opt_type if check else None
        entry["data_source"] = check.data_source if check else None
        entry["live_premium"] = live_premium
        entry["live_spot"] = live_spot
        entry["unrealized_pnl"] = unrealized_pnl
        # None = not applicable (manual trade, no GTT concept); True/False = checked.
        entry["has_gtt"] = has_gtt
        entry["distance_to_premium_stop"] = (
            live_premium - float(trade.stop_premium) if live_premium is not None else None
        )
        entry["distance_to_thesis_stop"] = (
            live_spot - float(trade.thesis_stop_level)
            if live_spot is not None and trade.thesis_stop_level is not None
            else None
        )
        entry["trading_days_to_time_stop"] = trading_days_to_expiry(today, trade.time_stop_date, holidays)
        entry["trading_days_to_expiry"] = trading_days_to_expiry(today, check.expiry, holidays) if check else None
        out.append(entry)
    return out


@router.get("/trades/closed")
def get_closed_trades(db: Session = Depends(get_db)):
    out = []
    for trade in repo.list_closed_trades(db):
        check = repo.get_check(db, trade.check_id)
        entry = trade_to_dict(trade)
        entry["symbol"] = check.symbol if check else None
        entry["expiry"] = check.expiry.isoformat() if check else None
        entry["strike"] = float(check.strike) if check else None
        entry["opt_type"] = check.opt_type if check else None
        entry["data_source"] = check.data_source if check else None
        out.append(entry)
    return out
