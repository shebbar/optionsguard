"""CSRF for /ui/* routes (loosen a stop, update settings). Per DESIGN.md
section 9: '/ui/* routes ... require a CSRF token issued to the browser
session. The MCP server only knows /api/v1' -- so the MCP server never has
a valid token and these routes stay out of its reach even if it tried."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request, Response

from optionguard.config import get_config

SESSION_COOKIE = "og_session"


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_token_for(session_token: str) -> str:
    key = get_config().secrets.optionguard_secret_key.encode()
    return hmac.new(key, session_token.encode(), hashlib.sha256).hexdigest()


def get_or_create_session_token(request: Request) -> tuple[str, bool]:
    """Returns (token, is_new). Doesn't touch the response -- a session
    token must be known *before* a template renders the CSRF field, but the
    Set-Cookie header can only be attached once the response object exists."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        return token, False
    return new_session_token(), True


def attach_session_cookie(response: Response, token: str, is_new: bool) -> None:
    if is_new:
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")


async def verify_csrf(request: Request) -> None:
    session_token = request.cookies.get(SESSION_COOKIE)
    if not session_token:
        raise HTTPException(status_code=403, detail="Missing session cookie.")
    sent = request.headers.get("X-CSRF-Token")
    if not sent:
        form = await request.form()
        sent = form.get("csrf_token")
    expected = csrf_token_for(session_token)
    if not sent or not hmac.compare_digest(str(sent), expected):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")
