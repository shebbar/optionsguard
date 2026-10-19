"""Stats grouped by verdict/exit_reason/month/symbol/override. Per
DESIGN.md sections 11 and 17 (M7)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from optionguard.db.models import Check, Override, Trade

GROUP_BY_OPTIONS = {"verdict", "exit_reason", "month", "symbol", "override"}


def compute_stats(
    db: Session, *, date_from: datetime | None = None, date_to: datetime | None = None, group_by: str = "verdict"
) -> list[dict]:
    if group_by not in GROUP_BY_OPTIONS:
        raise ValueError(f"group_by must be one of {sorted(GROUP_BY_OPTIONS)}")

    stmt = select(Trade, Check).join(Check, Trade.check_id == Check.id).where(Trade.status == "closed")
    if date_from:
        stmt = stmt.where(Trade.exit_time >= date_from)
    if date_to:
        stmt = stmt.where(Trade.exit_time <= date_to)
    rows = db.execute(stmt).all()

    overridden_trade_ids: set[int] = set()
    if group_by == "override":
        overridden_trade_ids = {
            o.trade_id for o in db.execute(select(Override).where(Override.trade_id.is_not(None))).scalars().all()
        }

    groups: dict[str, list[Trade]] = {}
    for trade, check in rows:
        if group_by == "verdict":
            key = check.verdict
        elif group_by == "exit_reason":
            key = trade.exit_reason or "unknown"
        elif group_by == "month":
            key = trade.exit_time.strftime("%Y-%m")
        elif group_by == "symbol":
            key = check.symbol
        else:  # override
            key = "overridden" if trade.id in overridden_trade_ids else "not_overridden"
        groups.setdefault(key, []).append(trade)

    out = []
    for key, trades in sorted(groups.items()):
        pnls = [float(t.pnl or 0) for t in trades]
        wins = sum(1 for p in pnls if p > 0)
        out.append({
            "group": key,
            "count": len(trades),
            "win_rate": wins / len(trades) if trades else 0.0,
            "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
            "total_pnl": sum(pnls),
        })
    return out
