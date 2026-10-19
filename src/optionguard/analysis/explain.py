"""`explanation_facts`: short factual sentences generated in code, which
Claude (via MCP) can quote rather than estimate. Per DESIGN.md section 11."""
from __future__ import annotations

from optionguard.analysis.metrics import CandidateRequest, Metrics
from optionguard.analysis.verdict import Flag


def build_explanation_facts(m: Metrics, req: CandidateRequest, flags: list[Flag], max_lots: int) -> list[str]:
    facts: list[str] = []
    if req.data_source == "mock":
        facts.append(
            "MOCK DATA: no real Kite credentials are configured, so every number above comes "
            "from a simulated market, not a real one. Do not use this to size or place a real trade."
        )
    facts.append(f"Spot is {m.spot:.2f}; premium is {m.premium:.2f} for the {req.strike:g} {req.opt_type}.")
    facts.append(f"Breakeven is {m.breakeven:.2f}, which is {m.breakeven_pct:+.1%} from spot.")
    if m.iv is not None:
        facts.append(f"Implied vol is {m.iv:.1%}" + (f", historical 20-day vol is {m.hv20:.1%}." if m.hv20 else "."))
    if m.prob_profit is not None:
        facts.append(f"Risk-neutral probability of profit at expiry is {m.prob_profit:.0%} -- not a forecast.")
    if m.prob_zero is not None:
        facts.append(f"Risk-neutral probability of expiring worthless is {m.prob_zero:.0%}.")
    if m.be_to_move_ratio is not None:
        facts.append(f"Breakeven needs {m.be_to_move_ratio:.1f}x the expected one-sigma move to be reached.")
    if m.theta_per_day_lot is not None:
        facts.append(f"Theta is costing about {m.theta_per_day_lot:.0f} per lot per trading day at current IV.")
    if m.daily_move_needed is not None:
        facts.append(
            f"Stock must move {m.daily_move_needed:.2f} ({m.daily_move_needed_pct:.1%}) "
            "your way per day just to offset decay."
        )
    facts.append(f"{m.trading_days_to_expiry} trading days to expiry.")
    if m.earnings_before_expiry:
        facts.append("Earnings fall before expiry -- IV often drops sharply right after results.")
    if max_lots > 0:
        facts.append(f"At current settings, max position size is {max_lots} lot(s).")
    else:
        facts.append("max_loss_per_trade can't fund even one lot at the current stop -- size is capped at zero.")
    for f in flags:
        facts.append(f"[{f.level.upper()} {f.code}] {f.message}")
    return facts
