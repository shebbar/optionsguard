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

from optionguard.api import events as events_api
from optionguard.api import journal as journal_api
from optionguard.api import settings as settings_api
from optionguard.api import status as status_api
from optionguard.api import trades as trades_api
from optionguard.api.checks import post_checks
from optionguard.api.errors import ApiError
from optionguard.api.schemas import (
    ChecksRequest, CandidateIn, EventIn, ExitIn, LoosenIn, ManualPayload, SettingsIn, TightenIn, TradeIn,
)
from optionguard.config import get_config
from optionguard.db.session import get_db
from optionguard.web.csrf import attach_session_cookie, csrf_token_for, get_or_create_session_token, verify_csrf

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

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
    return _render(request, "positions.html", {"positions": positions, "active": "positions"})


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


# ---- Journal screen -------------------------------------------------------

@router.get("/journal", response_class=HTMLResponse)
def page_journal(request: Request, group_by: str = "verdict", db: Session = Depends(get_db)):
    stats = journal_api.get_journal_stats(from_=None, to=None, group_by=group_by, db=db)
    checks = journal_api.get_journal_checks(from_=None, to=None, verdict=None, traded=None, db=db)
    return _render(request, "journal.html", {"stats": stats, "checks": checks, "group_by": group_by, "active": "journal"})


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
