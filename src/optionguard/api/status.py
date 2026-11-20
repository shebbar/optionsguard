"""GET /status -- Kite login state, month P&L, loss-limit headroom,
cooling-off state. Per DESIGN.md section 11."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from optionguard.config import get_config
from optionguard.db import repo
from optionguard.db.session import get_db
from optionguard.market.factory import is_kite_mock
from optionguard.orders.service import is_ip_error_message
from optionguard.rules import guardrails

router = APIRouter(prefix="/api/v1", tags=["status"])


@router.get("/status")
def get_status(db: Session = Depends(get_db)):
    cfg = get_config()
    holidays = set(cfg.holidays.dates)
    today = date.today()
    settings = repo.ensure_default_settings(db, cfg)

    month_pnl = guardrails.monthly_realised_pnl(db, today)
    in_cooloff, trigger_date = guardrails.cooloff_status(db, settings, today, holidays)

    mock_mode = is_kite_mock()
    kite_session = repo.get_kite_session(db)
    # The mock client never needs a real login -- treat it as always "logged
    # in" so the mock workflow isn't gated on a flow that only matters once
    # real Kite credentials exist.
    kite_logged_in = mock_mode or bool(
        kite_session and kite_session.access_token and kite_session.valid_for == today
    )

    # Section 7.8.2: ip_ok stays null until the first real order attempt,
    # then false only if that attempt's rejection looks like Kite's
    # static-IP error -- any other outcome (success, or a rejection for an
    # unrelated reason) proves the round-trip to Kite itself is fine.
    ip_ok = None
    last_ip_error = None
    latest_attempt = repo.get_latest_order_attempt(db)
    if latest_attempt is not None:
        if latest_attempt.status == "rejected" and is_ip_error_message(latest_attempt.kite_message):
            ip_ok = False
            last_ip_error = latest_attempt.kite_message
        else:
            ip_ok = True

    return {
        "kite_mode": "mock" if mock_mode else "live",
        "kite_mock_mode": mock_mode,
        "kite_logged_in": kite_logged_in,
        "kite_login_url": None if kite_logged_in else "/kite/login",
        "month_realised_pnl": month_pnl,
        "monthly_loss_limit": float(settings.monthly_loss_limit),
        "headroom": float(settings.monthly_loss_limit) + month_pnl,
        "in_cooloff": in_cooloff,
        "cooloff_trigger_date": trigger_date.isoformat() if trigger_date else None,
        "orders": {"enabled": cfg.orders.enabled, "ip_ok": ip_ok, "last_ip_error": last_ip_error},
    }
