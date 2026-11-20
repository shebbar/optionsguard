"""Single-leg alternative strikes. Per DESIGN.md section 7.4, revised: Kite/
mock mode only, same expiry and type, the nearest up to 6 listed strikes to
the requested one (either side, not just "between it and spot" -- that
window is empty whenever the requested strike is already the nearest or
2nd-nearest to spot, which is the common case, not an edge case). Top 2 by
prob_profit among max_lots >= 1 -- irrespective of verdict colour (red is
shown too, not excluded): a flag like NEAR_EXPIRY applies identically to
every strike on the same expiry, so excluding red candidates could silently
empty the whole list even when a materially better prob_profit exists right
next door. Spreads are v2."""
from __future__ import annotations

from dataclasses import replace
from datetime import date

from optionguard.analysis import sizing, verdict as verdict_mod
from optionguard.analysis.metrics import CandidateRequest, compute_metrics
from optionguard.config import Config
from optionguard.market.base import MarketProvider, NotAvailable

MAX_CANDIDATES = 6
TOP_N = 2


def compute_alternatives(
    provider: MarketProvider,
    req: CandidateRequest,
    *,
    config: Config,
    today: date,
    max_loss_per_trade: float,
    premium_stop_pct: float,
    dividends: list[tuple[date, float]] | None = None,
    has_earnings_before_expiry: bool = False,
) -> dict:
    if req.data_source == "manual":
        return {"candidates": [], "note": "connect Kite for alternatives"}

    try:
        all_strikes = provider.list_strikes(req.symbol, req.expiry)
    except NotAvailable:
        return {"candidates": [], "note": "connect Kite for alternatives"}

    candidates = [s for s in all_strikes if s != req.strike]
    candidates.sort(key=lambda s: abs(s - req.strike))
    candidates = candidates[:MAX_CANDIDATES]

    scored = []
    for strike in candidates:
        alt_req = replace(req, strike=strike, thesis_target=None, thesis_date=None)
        m = compute_metrics(
            provider, alt_req, config=config, today=today,
            dividends=dividends, has_earnings_before_expiry=has_earnings_before_expiry,
        )
        lots = sizing.max_lots(
            m.premium, m.lot_size,
            max_loss_per_trade=max_loss_per_trade, premium_stop_pct=premium_stop_pct,
        )
        flags = verdict_mod.compute_flags(m, alt_req, config.verdict, max_lots=lots, oi_context=None)
        v = verdict_mod.overall_verdict(flags)
        if lots >= 1:
            scored.append({
                "strike": strike,
                "verdict": v,
                "prob_profit": m.prob_profit,
                "max_lots": lots,
                "premium": m.premium,
                "breakeven": m.breakeven,
                "iv": m.iv,
            })

    scored.sort(key=lambda r: (r["prob_profit"] or 0), reverse=True)
    return {"candidates": scored[:TOP_N], "note": None}
