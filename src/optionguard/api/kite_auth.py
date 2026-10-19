"""GET /kite/login, GET /kite/callback. Per DESIGN.md section 8. Works
identically against the mock client or a real KiteConnect instance."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from optionguard.config import get_config
from optionguard.db import repo
from optionguard.db.session import get_db
from optionguard.market.factory import get_kite_client, is_kite_mock

router = APIRouter(tags=["kite-auth"])


@router.get("/kite/login")
def kite_login():
    client = get_kite_client()
    return RedirectResponse(client.login_url())


@router.get("/kite/callback")
def kite_callback(request_token: str, db: Session = Depends(get_db)):
    cfg = get_config()
    client = get_kite_client()
    session_data = client.generate_session(request_token, cfg.secrets.kite_api_secret)
    repo.set_kite_session(db, session_data["access_token"], date.today())
    return {"status": "ok", "mock_mode": is_kite_mock()}
