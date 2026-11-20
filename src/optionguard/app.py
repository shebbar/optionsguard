"""FastAPI app factory. Binds to 127.0.0.1 only -- see cli.py."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from optionguard.api import (
    checks, events, journal, kite_auth, option_theses, orders, settings as settings_api, status, trades,
)
from optionguard.api.errors import ApiError, api_error_handler, not_available_handler, rule_violation_handler
from optionguard.market.base import NotAvailable
from optionguard.rules.errors import RuleViolation
from optionguard.web.routes import router as web_router

STATIC_DIR = Path(__file__).parent / "web" / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="OptionGuard", version="0.1.0")

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RuleViolation, rule_violation_handler)
    app.add_exception_handler(NotAvailable, not_available_handler)

    # /api/v1/* -- JSON, MCP-reachable
    app.include_router(checks.router)
    app.include_router(trades.router)
    app.include_router(settings_api.router)
    app.include_router(events.router)
    app.include_router(journal.router)
    app.include_router(status.router)
    app.include_router(kite_auth.router)
    app.include_router(orders.router)
    app.include_router(option_theses.router)

    # /ui/* -- JSON, UI-only, CSRF-protected (settings loosening, stop loosening, cancel)
    app.include_router(trades.ui_router)
    app.include_router(settings_api.ui_router)
    app.include_router(orders.ui_router)
    app.include_router(option_theses.ui_router)

    # server-rendered HTML
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app
