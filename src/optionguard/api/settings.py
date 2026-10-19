"""GET /settings (read-only, MCP-reachable) and POST /ui/settings (UI-only,
loosening delayed to next trading day). Per DESIGN.md sections 9 and 11."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from optionguard.api.errors import ApiError
from optionguard.api.schemas import SettingsIn
from optionguard.api.serializers import settings_to_dict
from optionguard.config import get_config
from optionguard.db import repo
from optionguard.db.session import get_db
from optionguard.rules import guardrails

router = APIRouter(prefix="/api/v1", tags=["settings"])
ui_router = APIRouter(prefix="/ui", tags=["settings-ui"])


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    cfg = get_config()
    return settings_to_dict(repo.ensure_default_settings(db, cfg))


@ui_router.post("/settings")
def post_ui_settings(body: SettingsIn, db: Session = Depends(get_db)):
    cfg = get_config()
    holidays = set(cfg.holidays.dates)
    now = datetime.now(timezone.utc)
    old = repo.ensure_default_settings(db, cfg)

    new_fields = body.model_dump(exclude_none=True)
    if not new_fields:
        raise ApiError("NO_CHANGE", "No settings fields were provided.")

    effective_from = guardrails.settings_effective_from(old, new_fields, now, holidays)
    merged = {
        "max_loss_per_trade": new_fields.get("max_loss_per_trade", old.max_loss_per_trade),
        "monthly_loss_limit": new_fields.get("monthly_loss_limit", old.monthly_loss_limit),
        "premium_stop_pct": new_fields.get("premium_stop_pct", old.premium_stop_pct),
        "time_stop_days": new_fields.get("time_stop_days", old.time_stop_days),
        "cooloff_days": new_fields.get("cooloff_days", old.cooloff_days),
        "show_spreads": new_fields.get("show_spreads", old.show_spreads),
    }
    row = repo.insert_settings(db, effective_from=effective_from, **merged)
    out = settings_to_dict(row)
    out["loosening"] = guardrails.is_loosening(old, new_fields)
    return out
