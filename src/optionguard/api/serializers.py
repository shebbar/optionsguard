from __future__ import annotations

from optionguard.db.models import Check, Trade


def _f(v) -> float | None:
    return float(v) if v is not None else None


def check_to_dict(c: Check) -> dict:
    delta = _f(c.delta)
    daily_move_needed = _f(c.daily_move_needed)
    spot = _f(c.spot)
    return {
        "id": c.id,
        "created_at": c.created_at.isoformat(),
        "batch_id": c.batch_id,
        "symbol": c.symbol,
        "expiry": c.expiry.isoformat(),
        "strike": _f(c.strike),
        "opt_type": c.opt_type,
        "data_source": c.data_source,
        "spot": spot,
        "premium": _f(c.premium),
        "bid": _f(c.bid),
        "ask": _f(c.ask),
        "lot_size": c.lot_size,
        "iv": _f(c.iv),
        "hv20": _f(c.hv20),
        "hv_percentile": _f(c.hv_percentile),
        "breakeven": _f(c.breakeven),
        "breakeven_pct": _f(c.breakeven_pct),
        "expected_move_pct": _f(c.expected_move_pct),
        "prob_profit": _f(c.prob_profit),
        "prob_zero": _f(c.prob_zero),
        "prob_thesis": _f(c.prob_thesis),
        "theta_per_day_lot": _f(c.theta_per_day_lot),
        "delta": delta,
        "daily_move_needed": daily_move_needed,
        # Not a column -- derived from daily_move_needed/spot, per DESIGN.md 7.6.
        "daily_move_needed_pct": (daily_move_needed / spot) if daily_move_needed is not None and spot else None,
        "stretch_5d_pct": _f(c.stretch_5d_pct),
        "stretch_20dma_pct": _f(c.stretch_20dma_pct),
        "spread_pct": _f(c.spread_pct),
        "thesis_target": _f(c.thesis_target),
        "thesis_date": c.thesis_date.isoformat() if c.thesis_date else None,
        "verdict": c.verdict,
        "flags": c.flags,
        "max_lots": c.max_lots,
        "alternatives": c.alternatives,
        "oi_context": c.oi_context,
        # Not a column -- lives in raw.scenarios, per DESIGN.md 7.6.
        "scenarios": (c.raw or {}).get("scenarios", []),
        "expiry_settle": _f(c.expiry_settle),
        "settled_at": c.settled_at.isoformat() if c.settled_at else None,
    }


def trade_to_dict(t: Trade) -> dict:
    return {
        "id": t.id,
        "check_id": t.check_id,
        "entry_time": t.entry_time.isoformat(),
        "lots": t.lots,
        "lot_size": t.lot_size,
        "entry_premium": _f(t.entry_premium),
        "stop_premium": _f(t.stop_premium),
        "thesis_stop_level": _f(t.thesis_stop_level),
        "time_stop_date": t.time_stop_date.isoformat(),
        "status": t.status,
        "exit_time": t.exit_time.isoformat() if t.exit_time else None,
        "exit_premium": _f(t.exit_premium),
        "exit_reason": t.exit_reason,
        "charges": _f(t.charges),
        "pnl": _f(t.pnl),
    }


def settings_to_dict(s) -> dict:
    return {
        "id": s.id,
        "effective_from": s.effective_from.isoformat(),
        "max_loss_per_trade": _f(s.max_loss_per_trade),
        "monthly_loss_limit": _f(s.monthly_loss_limit),
        "premium_stop_pct": _f(s.premium_stop_pct),
        "time_stop_days": s.time_stop_days,
        "cooloff_days": s.cooloff_days,
        "show_spreads": s.show_spreads,
    }


def event_to_dict(e) -> dict:
    return {
        "id": e.id,
        "symbol": e.symbol,
        "event_type": e.event_type,
        "event_date": e.event_date.isoformat(),
        "dividend": _f(e.dividend),
    }
