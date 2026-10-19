"""The single module that decides what is allowed. Per DESIGN.md section 9.
Every function either returns normally (Ok) or raises RuleViolation."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from optionguard.db import repo
from optionguard.db.models import Check, Settings, Trade
from optionguard.pricing.calendar import IST, next_trading_day, trading_days_between
from optionguard.rules.errors import RuleViolation

CHECK_STALE_AFTER = timedelta(minutes=30)
MIN_REASON_LEN = 15


# ---- monthly P&L / cooling-off -----------------------------------------

def _month_bounds(as_of: date) -> tuple[datetime, datetime]:
    start = datetime(as_of.year, as_of.month, 1, tzinfo=IST)
    if as_of.month == 12:
        end = datetime(as_of.year + 1, 1, 1, tzinfo=IST)
    else:
        end = datetime(as_of.year, as_of.month + 1, 1, tzinfo=IST)
    return start, end


def trades_closed_this_month(db: Session, as_of: date) -> list[Trade]:
    start, end = _month_bounds(as_of)
    trades = repo.list_trades_closed_between(db, start, end)
    return sorted(trades, key=lambda t: t.exit_time)


def monthly_realised_pnl(db: Session, as_of: date) -> float:
    """Always computed from trades, never stored -- per DESIGN.md section 9."""
    return sum(float(t.pnl or 0) for t in trades_closed_this_month(db, as_of))


def cooloff_status(db: Session, settings: Settings, as_of: date, holidays: set[date]) -> tuple[bool, date | None]:
    """Cooling-off starts the day cumulative realised P&L for the calendar
    month first reaches <= -monthly_loss_limit, and lasts cooloff_days
    trading days from that trigger date."""
    trades = trades_closed_this_month(db, as_of)
    cum = 0.0
    trigger_date: date | None = None
    for t in trades:
        cum += float(t.pnl or 0)
        if trigger_date is None and cum <= -float(settings.monthly_loss_limit):
            trigger_date = t.exit_time.date()
    if trigger_date is None:
        return False, None
    elapsed = trading_days_between(trigger_date, as_of, holidays)
    return elapsed < settings.cooloff_days, trigger_date


# ---- record_trade --------------------------------------------------------

def guard_record_trade(
    db: Session,
    check: Check,
    lots: int,
    settings: Settings,
    now: datetime,
    holidays: set[date],
    override_reason: str | None,
) -> None:
    pnl = monthly_realised_pnl(db, now.date())
    if pnl <= -float(settings.monthly_loss_limit):
        raise RuleViolation("MONTHLY_LOSS_LIMIT", "Monthly realised loss has hit the limit.", overridable=False)

    in_cooloff, _ = cooloff_status(db, settings, now.date(), holidays)
    if in_cooloff:
        raise RuleViolation(
            "COOLING_OFF", "You're in a cooling-off period after hitting the monthly loss limit.", overridable=False
        )

    if lots > check.max_lots:
        raise RuleViolation(
            "OVER_MAX_LOTS", f"lots={lots} exceeds max_lots={check.max_lots} from the linked check.", overridable=False
        )

    age = now - check.created_at
    if age > CHECK_STALE_AFTER:
        raise RuleViolation("STALE_CHECK", "This check is more than 30 minutes old -- re-run /checks.", overridable=False)

    if check.verdict == "red":
        if not override_reason or len(override_reason) < MIN_REASON_LEN:
            raise RuleViolation(
                "RED_VERDICT",
                f"This candidate is a red verdict -- provide a reason (>= {MIN_REASON_LEN} chars) to override.",
                overridable=True,
            )


# ---- tighten_stop / loosen_stop -----------------------------------------

def guard_tighten_stop(
    *,
    old_stop_premium: float,
    new_stop_premium: float | None = None,
    old_thesis_stop_level: float | None = None,
    new_thesis_stop_level: float | None = None,
    old_time_stop_date: date,
    new_time_stop_date: date | None = None,
    spot: float | None = None,
) -> None:
    changed = False

    if new_stop_premium is not None:
        changed = True
        if new_stop_premium <= old_stop_premium:
            raise RuleViolation(
                "NOT_TIGHTER", "New premium stop must be higher (more protective) than the current one.", overridable=False
            )

    if new_thesis_stop_level is not None:
        changed = True
        if spot is None:
            raise RuleViolation("NOT_TIGHTER", "Need the current spot to judge a thesis-stop tighten.", overridable=False)
        old_dist = abs(old_thesis_stop_level - spot) if old_thesis_stop_level is not None else float("inf")
        if abs(new_thesis_stop_level - spot) >= old_dist:
            raise RuleViolation(
                "NOT_TIGHTER", "New thesis stop level must be closer to spot than the current one.", overridable=False
            )

    if new_time_stop_date is not None:
        changed = True
        if new_time_stop_date >= old_time_stop_date:
            raise RuleViolation(
                "NOT_TIGHTER", "New time-stop date must be earlier than the current one.", overridable=False
            )

    if not changed:
        raise RuleViolation("NO_CHANGE", "No stop field was provided to tighten.", overridable=False)


def guard_loosen_stop(reason: str | None) -> None:
    """Web UI only -- no API route reachable from MCP. Caller is responsible
    for keeping this off /api/v1."""
    if not reason or len(reason) < MIN_REASON_LEN:
        raise RuleViolation(
            "REASON_REQUIRED", f"Loosening a stop needs a reason of at least {MIN_REASON_LEN} characters.", overridable=False
        )


# ---- update_settings -----------------------------------------------------

LOOSENING_FIELDS_HIGHER_IS_LOOSER = {"max_loss_per_trade", "monthly_loss_limit", "premium_stop_pct"}
LOOSENING_FIELDS_LOWER_IS_LOOSER = {"time_stop_days", "cooloff_days"}


def is_loosening(old: Settings, new_fields: dict) -> bool:
    for field in LOOSENING_FIELDS_HIGHER_IS_LOOSER:
        if field in new_fields and float(new_fields[field]) > float(getattr(old, field)):
            return True
    for field in LOOSENING_FIELDS_LOWER_IS_LOOSER:
        if field in new_fields and float(new_fields[field]) < float(getattr(old, field)):
            return True
    return False


def settings_effective_from(old: Settings, new_fields: dict, now: datetime, holidays: set[date]) -> datetime:
    """Web UI only. A loosening change takes effect next trading day 09:00
    IST; a tightening (or neutral) change is effective immediately."""
    if is_loosening(old, new_fields):
        tomorrow = next_trading_day(now.date(), holidays)
        return datetime.combine(tomorrow, time(9, 0), tzinfo=IST)
    return now


# ---- clear_cooloff -------------------------------------------------------

def guard_clear_cooloff() -> None:
    raise RuleViolation("NOT_AVAILABLE", "Clearing cooling-off is not available.", overridable=False)
