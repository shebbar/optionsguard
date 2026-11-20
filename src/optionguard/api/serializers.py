from __future__ import annotations

from optionguard.db.models import (
    Check, OptionThesis, OptionThesisPolicyResult, OptionThesisScore, OrderIntent, Trade,
)


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
        # Section 7.8, added.
        "entry_intent_id": t.entry_intent_id,
        "exit_intent_id": t.exit_intent_id,
        "gtt_id": t.gtt_id,
        "gtt_status": t.gtt_status,
    }


def order_intent_to_dict(i: OrderIntent) -> dict:
    return {
        "id": i.id,
        "created_at": i.created_at.isoformat(),
        "kind": i.kind,
        "check_id": i.check_id,
        "trade_id": i.trade_id,
        "option_thesis_id": i.option_thesis_id,
        "tradingsymbol": i.tradingsymbol,
        "quantity": i.quantity,
        "order_type": i.order_type,
        "price": _f(i.price),
        "stop_premium": _f(i.stop_premium),
        "target_premium": _f(i.target_premium),
        "override_reason": i.override_reason,
        "status": i.status,
        "order_id": i.order_id,
        "filled_qty": i.filled_qty,
        "average_price": _f(i.average_price),
        "kite_message": i.kite_message,
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


def option_thesis_to_dict(t: OptionThesis) -> dict:
    return {
        "id": t.id,
        "created_at": t.created_at.isoformat(),
        "symbol": t.symbol,
        "expiry": t.expiry.isoformat(),
        "strike": _f(t.strike),
        "opt_type": t.opt_type,
        "opt_instrument_token": t.opt_instrument_token,
        "mode": t.mode,
        "data_source": t.data_source,
        "sector": t.sector,
        "sector_index": t.sector_index,
        "why": t.why,
        "check_id": t.check_id,
        "verdict": t.verdict,
        "check_snapshot": t.check_snapshot,
        "override_id": t.override_id,
        "lot_size": t.lot_size,
        "lots": t.lots,
        "entry_premium": _f(t.entry_premium),
        "target_premium": _f(t.target_premium),
        "stop_premium": _f(t.stop_premium),
        "duration_days": t.duration_days,
        "end_date": t.end_date.isoformat(),
        "reward_risk": _f(t.reward_risk),
        "spot_for_target": _f(t.spot_for_target),
        "spot_for_stop": _f(t.spot_for_stop),
        "mkt_prob_target_first": _f(t.mkt_prob_target_first),
        "mkt_prob_stop_first": _f(t.mkt_prob_stop_first),
        "mkt_prob_time_exit": _f(t.mkt_prob_time_exit),
        "spot_entry": _f(t.spot_entry),
        "sector_index_entry": _f(t.sector_index_entry),
        "market_index_entry": _f(t.market_index_entry),
        "entry_flags": t.entry_flags,
        "trade_id": t.trade_id,
        "notes": t.notes,
        "status": t.status,
        "outcome": t.outcome,
        "exit_time": t.exit_time.isoformat() if t.exit_time else None,
        "exit_premium": _f(t.exit_premium),
        "manual_exit_reason": t.manual_exit_reason,
        "manual_exit_entered_at": t.manual_exit_entered_at.isoformat() if t.manual_exit_entered_at else None,
        "cancelled_at": t.cancelled_at.isoformat() if t.cancelled_at else None,
        "cancel_reason": t.cancel_reason,
    }


def option_thesis_score_to_dict(s: OptionThesisScore) -> dict:
    return {
        "thesis_id": s.thesis_id,
        "scored_at": s.scored_at.isoformat(),
        "score_version": s.score_version,
        "days_held": s.days_held,
        "ret_pct": _f(s.ret_pct),
        "pnl": _f(s.pnl),
        "charges": _f(s.charges),
        "r_multiple": _f(s.r_multiple),
        "mfe_pct": _f(s.mfe_pct),
        "mfe_time": s.mfe_time.isoformat() if s.mfe_time else None,
        "mae_pct": _f(s.mae_pct),
        "mae_time": s.mae_time.isoformat() if s.mae_time else None,
        "target_after_stop": s.target_after_stop,
        "planned_outcome": s.planned_outcome,
        "planned_r": _f(s.planned_r),
        "spot_ret_pct": _f(s.spot_ret_pct),
        "sector_ret_pct": _f(s.sector_ret_pct),
        "market_ret_pct": _f(s.market_ret_pct),
        "bars": s.bars,
        "flags": s.flags,
        "path": s.path,
    }


def option_thesis_policy_result_to_dict(r: OptionThesisPolicyResult) -> dict:
    return {
        "thesis_id": r.thesis_id,
        "policy": r.policy,
        "policy_params": r.policy_params,
        "outcome": r.outcome,
        "exit_time": r.exit_time.isoformat() if r.exit_time else None,
        "exit_premium": _f(r.exit_premium),
        "days_held": r.days_held,
        "pnl": _f(r.pnl),
        "r_multiple": _f(r.r_multiple),
        "pending": r.pending,
        "scored_at": r.scored_at.isoformat(),
    }
