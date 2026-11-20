"""Server-rendered Jinja2 + HTMX UI. Per DESIGN.md section 13. Every
state-changing action here calls the exact same api/* functions the JSON API
and MCP server use -- guardrails are enforced once, in rules/guardrails.py,
never re-implemented here (DESIGN.md principle 1)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from optionguard.api import checks as checks_api
from optionguard.api import events as events_api
from optionguard.api import journal as journal_api
from optionguard.api import option_theses as option_theses_api
from optionguard.api import orders as orders_api
from optionguard.api import settings as settings_api
from optionguard.api import status as status_api
from optionguard.api import trades as trades_api
from optionguard.api.checks import post_checks
from optionguard.api.errors import ApiError
from optionguard.api.schemas import (
    ChecksRequest, CandidateIn, EventIn, ExitIn, LoosenIn, ManualPayload, OptionThesisCancelIn, OptionThesisExitIn,
    OptionThesisIn, OptionThesisLinkTradeIn, OptionThesisNotesIn, OptionThesisPreviewIn, OrderConfirmIn,
    OrderModifyIn, OrderReviewIn, SettingsIn, TightenIn, TradeIn,
)
from optionguard.api.serializers import check_to_dict, trade_to_dict
from optionguard.config import get_config
from optionguard.db import repo
from optionguard.db.session import get_db
from optionguard.orders.service import provider_for
from optionguard.web.csrf import attach_session_cookie, csrf_token_for, get_or_create_session_token, verify_csrf

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["month_abbr"] = lambda iso: date.fromisoformat(iso).strftime("%b").upper()

router = APIRouter()


def _render(request: Request, name: str, context: dict) -> HTMLResponse:
    # The session token must be settled *before* the template renders so the
    # CSRF field matches the Set-Cookie header this same response sends --
    # not the (possibly absent) cookie the request arrived with.
    token, is_new = get_or_create_session_token(request)
    context = {**context, "csrf_token": csrf_token_for(token)}
    resp = templates.TemplateResponse(request=request, name=name, context=context)
    attach_session_cookie(resp, token, is_new)
    return resp


# ---- Check screen -------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def page_index(request: Request, db: Session = Depends(get_db)):
    status = status_api.get_status(db)
    return _render(request, "index.html", {"status": status, "active": "check"})


@router.get("/status-html", response_class=HTMLResponse)
def status_html(request: Request, db: Session = Depends(get_db)):
    status = status_api.get_status(db)
    return _render(request, "_statusbar.html", {"status": status})


@router.post("/checks/prepare", response_class=HTMLResponse)
async def checks_prepare(request: Request):
    form = await request.form()
    text = form.get("text", "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return _render(request, "_manual_rows.html", {"lines": lines})


@router.post("/checks-html", response_class=HTMLResponse)
async def checks_html(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    source = form.get("source", "kite")
    text = form.get("text", "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    candidates = []
    for i, line in enumerate(lines):
        manual = None
        if source == "manual":
            try:
                manual = ManualPayload(
                    spot=form.get(f"manual_spot_{i}"),
                    premium=form.get(f"manual_premium_{i}"),
                    lot_size=form.get(f"manual_lot_size_{i}"),
                    expiry=form.get(f"manual_expiry_{i}"),
                    bid=form.get(f"manual_bid_{i}") or None,
                    ask=form.get(f"manual_ask_{i}") or None,
                    hv20=form.get(f"manual_hv20_{i}") or None,
                )
            except Exception as exc:
                return _render(request, "_results.html", {"error": f"{line}: {exc}"})
        candidates.append(CandidateIn(text=line, manual=manual))

    if not candidates:
        return _render(request, "_results.html", {"error": "No candidates entered."})

    try:
        body = ChecksRequest(candidates=candidates, source=source)
        out = post_checks(body, db)
    except ApiError as exc:
        return _render(request, "_results.html", {"error": f"{exc.code}: {exc.message}"})

    return _render(request, "_results.html", {"results": out["results"], "ranking": out["ranking"]})


@router.post("/trades-html", response_class=HTMLResponse)
async def trades_html(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        body = TradeIn(
            check_id=int(form["check_id"]), lots=int(form["lots"]), entry_premium=float(form["entry_premium"]),
            override_reason=form.get("override_reason") or None,
        )
        trade = trades_api.post_trade(body, db)
    except ApiError as exc:
        return _render(request, "_trade_result.html", {"error": {"code": exc.code, "message": exc.message}})
    return _render(request, "_trade_result.html", {"trade": trade})


# ---- Positions screen -----------------------------------------------------

@router.get("/positions", response_class=HTMLResponse)
def page_positions(request: Request, db: Session = Depends(get_db)):
    positions = trades_api.get_positions(db)
    closed_trades = trades_api.get_closed_trades(db)
    return _render(request, "positions.html", {"positions": positions, "closed_trades": closed_trades, "active": "positions"})


@router.post("/positions/{trade_id}/tighten-html", response_class=HTMLResponse)
async def positions_tighten(trade_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        body = TightenIn(
            stop_premium=float(form["stop_premium"]) if form.get("stop_premium") else None,
            time_stop_date=date.fromisoformat(form["time_stop_date"]) if form.get("time_stop_date") else None,
        )
        trade = trades_api.post_tighten(trade_id, body, db)
    except ApiError as exc:
        return _render(request, "_action_result.html", {"error": {"code": exc.code, "message": exc.message}})
    return _render(request, "_action_result.html", {"trade": trade})


@router.post("/positions/{trade_id}/loosen-html", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def positions_loosen(trade_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        body = LoosenIn(
            stop_premium=float(form["stop_premium"]) if form.get("stop_premium") else None,
            time_stop_date=date.fromisoformat(form["time_stop_date"]) if form.get("time_stop_date") else None,
            reason=form["reason"],
        )
        trade = trades_api.post_loosen(trade_id, body, db)
    except ApiError as exc:
        return _render(request, "_action_result.html", {"error": {"code": exc.code, "message": exc.message}})
    return _render(request, "_action_result.html", {"trade": trade})


@router.post("/positions/{trade_id}/exit-html", response_class=HTMLResponse)
async def positions_exit(trade_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        body = ExitIn(exit_premium=float(form["exit_premium"]), exit_reason=form["exit_reason"])
        trade = trades_api.post_exit(trade_id, body, db)
    except ApiError as exc:
        return _render(request, "_action_result.html", {"error": {"code": exc.code, "message": exc.message}})
    return _render(request, "_action_result.html", {"trade": trade})


@router.post("/trades/{trade_id}/gtt-html", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
def retry_gtt_html(trade_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        trade = orders_api.post_retry_gtt(trade_id, db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    return HTMLResponse(f'<div class="muted">GTT placed (id {trade["gtt_id"]}).</div>')


# ---- Order ticket (section 7.8, added) -------------------------------------

def _quote_for_check(db: Session, check) -> dict:
    provider = provider_for(db, check.data_source)
    q = provider.option_quote(check.symbol, check.expiry, float(check.strike), check.opt_type)
    spread_pct = (q.ask - q.bid) / q.last_price if q.bid and q.ask and q.last_price else None
    return {"last_price": q.last_price, "bid": q.bid, "ask": q.ask, "spread_pct": spread_pct}


@router.get("/order", response_class=HTMLResponse)
def page_order(
    request: Request, check_id: int | None = None, trade_id: int | None = None, thesis_id: int | None = None,
    db: Session = Depends(get_db),
):
    cfg = get_config()
    status = status_api.get_status(db)
    ctx = {"status": status, "active": "order", "orders_cfg": cfg.orders}

    if check_id is not None:
        check = repo.get_check(db, check_id)
        if check is None:
            return _render(request, "order.html", {**ctx, "error": f"check {check_id} not found"})
        try:
            quote = _quote_for_check(db, check)
        except Exception as exc:  # noqa: BLE001
            return _render(request, "order.html", {**ctx, "error": str(exc)})

        thesis = None
        if thesis_id is not None:
            thesis = option_theses_api.get_option_thesis(thesis_id, db=db)
        return _render(request, "order.html", {
            **ctx, "kind": "entry", "check": check_to_dict(check), "quote": quote,
            "quote_query": f"check_id={check.id}", "thesis": thesis,
        })

    if trade_id is not None:
        trade = repo.get_trade(db, trade_id)
        if trade is None:
            return _render(request, "order.html", {**ctx, "error": f"trade {trade_id} not found"})
        check = repo.get_check(db, trade.check_id)
        try:
            quote = _quote_for_check(db, check)
        except Exception as exc:  # noqa: BLE001
            return _render(request, "order.html", {**ctx, "error": str(exc)})
        return _render(request, "order.html", {
            **ctx, "kind": "exit", "check": check_to_dict(check), "trade": trade_to_dict(trade), "quote": quote,
            "quote_query": f"trade_id={trade.id}",
        })

    return _render(request, "order.html", {**ctx, "error": "check_id or trade_id query parameter is required"})


@router.get("/order/quote-html", response_class=HTMLResponse)
def order_quote_html(request: Request, check_id: int | None = None, trade_id: int | None = None, db: Session = Depends(get_db)):
    check = repo.get_check(db, check_id) if check_id else repo.get_check(db, repo.get_trade(db, trade_id).check_id)
    try:
        quote = _quote_for_check(db, check)
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(f'<div class="error">{exc}</div>')
    quote_query = f"check_id={check_id}" if check_id else f"trade_id={trade_id}"
    return _render(request, "_order_quote.html", {"quote": quote, "quote_query": quote_query})


@router.post("/order/review-html", response_class=HTMLResponse)
async def order_review_html(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        body = OrderReviewIn(
            kind=form["kind"],
            check_id=int(form["check_id"]) if form.get("check_id") else None,
            trade_id=int(form["trade_id"]) if form.get("trade_id") else None,
            lots=int(form["lots"]) if form.get("lots") else None,
            quantity=int(form["quantity"]) if form.get("quantity") else None,
            price=float(form["price"]),
            order_type=form.get("order_type", "LIMIT"),
            stop_premium=float(form["stop_premium"]) if form.get("stop_premium") else None,
            target_premium=float(form["target_premium"]) if form.get("target_premium") else None,
            override_reason=form.get("override_reason") or None,
            thesis_id=int(form["thesis_id"]) if form.get("thesis_id") else None,
        )
        intent = orders_api.post_order_review(body, db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    except Exception as exc:  # noqa: BLE001 -- pydantic/casting errors from raw form parsing
        return HTMLResponse(f'<div class="error">{exc}</div>')
    return _render(request, "_order_review.html", {"intent": intent})


@router.post("/order/confirm-html", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def order_confirm_html(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    cfg = get_config()
    try:
        body = OrderConfirmIn(intent_id=int(form["intent_id"]))
        intent = orders_api.post_order_confirm(body, db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    return _render(request, "_order_status.html", {"intent": intent, "orders_cfg": cfg.orders})


@router.get("/order/{intent_id}/status-html", response_class=HTMLResponse)
def order_status_html(intent_id: int, request: Request, db: Session = Depends(get_db)):
    cfg = get_config()
    try:
        intent = orders_api.get_order_status(intent_id, db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    return _render(request, "_order_status.html", {"intent": intent, "orders_cfg": cfg.orders})


@router.post("/order/{intent_id}/modify-html", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def order_modify_html(intent_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    cfg = get_config()
    try:
        body = OrderModifyIn(price=float(form["price"]) if form.get("price") else None)
        intent = orders_api.post_order_modify(intent_id, body, db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    return _render(request, "_order_status.html", {"intent": intent, "orders_cfg": cfg.orders})


@router.post("/order/{intent_id}/cancel-html", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
def order_cancel_html(intent_id: int, request: Request, db: Session = Depends(get_db)):
    cfg = get_config()
    try:
        intent = orders_api.post_order_cancel(intent_id, db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    return _render(request, "_order_status.html", {"intent": intent, "orders_cfg": cfg.orders})


# ---- Journal screen -------------------------------------------------------

@router.get("/journal", response_class=HTMLResponse)
def page_journal(request: Request, group_by: str = "verdict", acted_only: bool = True, db: Session = Depends(get_db)):
    stats = journal_api.get_journal_stats(from_=None, to=None, group_by=group_by, db=db)
    checks = journal_api.get_journal_checks(
        from_=None, to=None, verdict=None, traded=None, acted=acted_only or None, db=db,
    )
    referenced = repo.set_of_referenced_check_ids(db)
    for c in checks:
        c["deletable"] = c["id"] not in referenced
    return _render(request, "journal.html", {
        "stats": stats, "checks": checks, "group_by": group_by, "acted_only": acted_only, "active": "journal",
    })


@router.post("/journal/checks/{check_id}/delete-html", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
def journal_check_delete_html(check_id: int, db: Session = Depends(get_db)):
    try:
        checks_api.delete_check(check_id, db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    return HTMLResponse("")


# ---- Settings screen ------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse)
def page_settings(request: Request, db: Session = Depends(get_db)):
    cfg = get_config()
    settings = settings_api.get_settings(db)
    events = events_api.get_events(db=db)
    status = status_api.get_status(db)
    return _render(request, "settings.html", {
        "settings": settings, "events": events, "status": status,
        "holidays": [d.isoformat() for d in cfg.holidays.dates],
        "active": "settings",
    })


@router.post("/settings-html", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def settings_html(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    fields = {}
    for key in ("max_loss_per_trade", "monthly_loss_limit", "premium_stop_pct", "time_stop_days", "cooloff_days"):
        if form.get(key):
            fields[key] = form[key]
    try:
        body = SettingsIn(**fields)
        row = settings_api.post_ui_settings(body, db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    note = " (effective next trading day 09:00 IST)" if row.get("loosening") else " (effective immediately)"
    return HTMLResponse(f'<div class="muted">Settings updated{note}.</div>')


@router.post("/events-html", response_class=HTMLResponse)
async def events_html(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        body = EventIn(
            symbol=form["symbol"], event_type=form["event_type"], event_date=form["event_date"],
            dividend=float(form["dividend"]) if form.get("dividend") else None,
        )
        events_api.post_event(body, db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    return HTMLResponse('<div class="muted">Event added.</div>')


# ---- Option theses screen (section 7.9, added) -----------------------------

def _svg_option_path_chart(path: list[dict], stop: float, entry: float, target: float, exit_time: str | None) -> dict | None:
    """Pre-computes SVG points server-side, for the option's own premium
    path (15-minute closes) -- horizontal lines at stop/entry/target, the
    exit point marked if the index is known."""
    if not path:
        return None
    closes = [p["c"] for p in path] + [stop, entry, target]
    lo, hi = min(closes), max(closes)
    span = (hi - lo) or 1.0
    width, height, pad = 600, 220, 10
    n = len(path)

    def xy(i: int, value: float) -> tuple[float, float]:
        x = pad + i / max(n - 1, 1) * (width - 2 * pad)
        y = pad + (1 - (value - lo) / span) * (height - 2 * pad)
        return x, y

    points = " ".join(f"{xy(i, p['c'])[0]:.1f},{xy(i, p['c'])[1]:.1f}" for i, p in enumerate(path))
    exit_xy = None
    if exit_time:
        idx = next((i for i, p in enumerate(path) if p["ts"] >= exit_time), n - 1)
        exit_xy = xy(idx, path[idx]["c"])

    return {
        "width": width, "height": height, "points": points,
        "stop_y": xy(0, stop)[1], "entry_y": xy(0, entry)[1], "target_y": xy(0, target)[1],
        "exit_x": exit_xy[0] if exit_xy else None, "exit_y": exit_xy[1] if exit_xy else None,
    }


@router.get("/option-theses", response_class=HTMLResponse)
def page_option_theses(request: Request, mode: str = "all", group_by: str = "none", db: Session = Depends(get_db)):
    cfg = get_config()
    status = status_api.get_status(db)
    sync_latest = option_theses_api.get_sync_latest(db)
    open_theses = option_theses_api.get_option_theses(status="open", db=db)
    for t in open_theses:
        t["interim"] = option_theses_api.get_option_thesis(t["id"], db=db)["interim"]
    scored_theses = option_theses_api.get_option_theses(status="scored", db=db)
    for t in scored_theses:
        detail = option_theses_api.get_option_thesis(t["id"], db=db)
        t["score"] = detail["score"]
        t["policies"] = detail["policies"]
        if t["score"]:
            t["chart"] = _svg_option_path_chart(
                t["score"]["path"], t["stop_premium"], t["entry_premium"], t["target_premium"], t["exit_time"],
            )
    scorecard = option_theses_api.get_option_theses_scorecard(mode=mode, group_by=group_by, db=db)

    from optionguard.pricing.calendar import trading_days_to_expiry

    today = date.today()
    holidays = set(cfg.holidays.dates)
    expiring_unsynced = [
        t for t in open_theses
        if trading_days_to_expiry(today, date.fromisoformat(t["expiry"]), holidays) <= cfg.option_theses.expiry_warning_days
    ]

    return _render(request, "option_theses.html", {
        "status": status, "active": "option_theses", "sync_latest": sync_latest,
        "open_theses": open_theses, "scored_theses": scored_theses, "scorecard": scorecard,
        "mode": mode, "group_by": group_by, "sectors": cfg.sectors,
        "expiring_unsynced": expiring_unsynced, "expiry_warning_days": cfg.option_theses.expiry_warning_days,
        "prefill_contract": request.query_params.get("contract", ""),
        "prefill_check_id": request.query_params.get("check_id", ""),
    })


@router.post("/option-theses/preview-html", response_class=HTMLResponse)
async def option_theses_preview_html(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        body = OptionThesisPreviewIn(
            contract=form["contract"], source=form.get("source", "kite"),
            target_premium=float(form["target_premium"]), stop_premium=float(form["stop_premium"]),
            duration_days=int(form["duration_days"]), lots=int(form.get("lots") or 1),
        )
        preview = option_theses_api.post_option_thesis_preview(body, db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    except Exception as exc:  # noqa: BLE001 -- raw form parsing (empty/partial fields while typing)
        return HTMLResponse(f'<div class="muted">{exc}</div>')
    return _render(request, "_option_thesis_preview.html", {"p": preview})


@router.post("/option-theses-html", response_class=HTMLResponse)
async def option_theses_create_html(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    if not form.get("check_id"):
        return HTMLResponse('<div class="error">Run a check for this contract first -- Save needs a check_id.</div>')
    try:
        body = OptionThesisIn(
            check_id=int(form["check_id"]), mode=form.get("mode", "paper"), why=form["why"],
            target_premium=float(form["target_premium"]), stop_premium=float(form["stop_premium"]),
            duration_days=int(form["duration_days"]), lots=int(form.get("lots") or 1),
            sector=form.get("sector") or None, override_reason=form.get("override_reason") or None,
            notes=form.get("notes") or None,
        )
        created = option_theses_api.post_option_thesis(body, db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    except Exception as exc:  # noqa: BLE001 -- pydantic/validation errors from raw form parsing
        return HTMLResponse(f'<div class="error">{exc}</div>')
    if created.get("next"):
        return HTMLResponse(f'<div class="muted">Thesis #{created["id"]} saved. <a class="btn" href="{created["next"]}">Buy &rarr;</a></div>')
    return HTMLResponse(f'<div class="muted">Thesis #{created["id"]} saved.</div>')


@router.post("/option-theses/resolve-html", response_class=HTMLResponse)
def option_theses_resolve_html(request: Request, db: Session = Depends(get_db)):
    try:
        result = option_theses_api.post_option_theses_resolve(db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    return HTMLResponse(f'<div class="muted">Resolve &amp; score {result["status"]}: {result["counts"]}</div>')


@router.get("/option-theses/scorecard-html", response_class=HTMLResponse)
def option_theses_scorecard_html(request: Request, mode: str = "all", group_by: str = "none", db: Session = Depends(get_db)):
    scorecard = option_theses_api.get_option_theses_scorecard(mode=mode, group_by=group_by, db=db)
    return _render(request, "_option_theses_scorecard.html", {"scorecard": scorecard, "mode": mode, "group_by": group_by})


@router.post("/option-theses/{thesis_id}/cancel-html", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def option_theses_cancel_html(thesis_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        body = OptionThesisCancelIn(reason=form["reason"])
        option_theses_api.post_option_thesis_cancel(thesis_id, body, db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    return HTMLResponse('<div class="muted">Cancelled.</div>')


@router.post("/option-theses/{thesis_id}/exit-html", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def option_theses_exit_html(thesis_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        body = OptionThesisExitIn(
            exit_time=form["exit_time"], price=float(form["price"]), reason=form["reason"],
            text=form.get("text") or None,
        )
        option_theses_api.post_option_thesis_exit(thesis_id, body, db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    return HTMLResponse('<div class="muted">Exit recorded.</div>')


@router.post("/option-theses/{thesis_id}/link-trade-html", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def option_theses_link_trade_html(thesis_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        body = OptionThesisLinkTradeIn(trade_id=int(form["trade_id"]))
        option_theses_api.post_option_thesis_link_trade(thesis_id, body, db)
    except ApiError as exc:
        return HTMLResponse(f'<div class="error">{exc.code}: {exc.message}</div>')
    return HTMLResponse('<div class="muted">Linked.</div>')
