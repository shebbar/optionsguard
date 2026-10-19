"""Single-leg alternative strikes. Per DESIGN.md section 7.4: Kite/mock mode
only, same expiry and type, up to 6 strikes between the requested strike and
at-the-money, top 2 by prob_profit among verdict != red and max_lots >= 1.
Spreads are v2."""
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
        spot = provider.spot(req.symbol)
        all_strikes = provider.list_strikes(req.symbol, req.expiry)
    except NotAvailable:
        return {"candidates": [], "note": "connect Kite for alternatives"}

    lo, hi = sorted([req.strike, spot])
    candidates = [s for s in all_strikes if lo <= s <= hi and s != req.strike]
    candidates.sort(key=lambda s: abs(s - spot))
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
        if v != "red" and lots >= 1:
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
