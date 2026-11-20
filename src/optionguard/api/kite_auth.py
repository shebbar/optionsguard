"""GET /kite/login, GET /kite/callback. Per DESIGN.md section 8. Works
identically against the mock client or a real KiteConnect instance."""
from __future__ import annotations

import logging
from datetime import date
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from optionguard.config import get_config
from optionguard.db import repo
from optionguard.db.session import get_db
from optionguard.market.factory import get_kite_client
from optionguard.sync import SyncAlreadyRunningError, run_sync

router = APIRouter(tags=["kite-auth"])
logger = logging.getLogger(__name__)

LOGIN_NEXT_COOKIE = "og_login_next"


@router.get("/kite/login")
def kite_login(request: Request):
    client = get_kite_client()
    resp = RedirectResponse(client.login_url())
    # Remember which page the login was started from (Kite's hosted login
    # flow has no way to round-trip custom state through its own redirect),
    # so the callback can send the browser back there instead of showing a
    # bare JSON page.
    referer = request.headers.get("referer")
    if referer:
        next_path = urlparse(referer).path or "/"
        resp.set_cookie(LOGIN_NEXT_COOKIE, next_path, max_age=600, httponly=True, samesite="lax")
    return resp


def _run_sync_after_login() -> None:
    try:
        run_sync(trigger="login")
    except SyncAlreadyRunningError:
        pass
    except Exception:  # noqa: BLE001 -- a background task's failure must not surface to the login redirect
        logger.exception("post-login sync failed")


@router.get("/kite/callback")
def kite_callback(request_token: str, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    cfg = get_config()
    client = get_kite_client()
    session_data = client.generate_session(request_token, cfg.secrets.kite_api_secret)
    repo.set_kite_session(db, session_data["access_token"], date.today())
    # Section 7.7.5: a sync runs automatically after login, as a background
    # task so the login redirect itself isn't delayed by it.
    background_tasks.add_task(_run_sync_after_login)
    next_path = request.cookies.get(LOGIN_NEXT_COOKIE) or "/"
    resp = RedirectResponse(next_path)
    resp.delete_cookie(LOGIN_NEXT_COOKIE)
    return resp
