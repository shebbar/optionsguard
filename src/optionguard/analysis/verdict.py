"""Verdict engine. Pure function of metrics + thresholds -> flags + verdict.
Per DESIGN.md section 7.2: red dominates amber; thresholds live in
config.toml [verdict]."""
from __future__ import annotations

from dataclasses import dataclass

from optionguard.analysis.metrics import CandidateRequest, Metrics
from optionguard.config import VerdictConfig


@dataclass(slots=True)
class Flag:
    code: str
    level: str  # "red" | "amber"
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "level": self.level, "message": self.message}


def compute_flags(
    m: Metrics,
    req: CandidateRequest,
    cfg: VerdictConfig,
    *,
    max_lots: int,
    oi_context: dict | None = None,
) -> list[Flag]:
    flags: list[Flag] = []

    if m.prob_profit is not None:
        if m.prob_profit < cfg.low_prob_red:
            flags.append(Flag("LOW_PROB", "red", f"prob_profit {m.prob_profit:.0%} is below {cfg.low_prob_red:.0%}."))
        elif m.prob_profit < cfg.low_prob_amber:
            flags.append(Flag("LOW_PROB_SOFT", "amber", f"prob_profit {m.prob_profit:.0%} is only just above the red line."))

    if m.be_to_move_ratio is not None and m.be_to_move_ratio > cfg.be_to_move_red:
        flags.append(Flag(
            "FAR_BREAKEVEN", "red",
            f"Breakeven needs {m.be_to_move_ratio:.1f}x the expected 1-sigma move to be reached.",
        ))

    if m.stretch_5d_pct is not None or m.stretch_20dma_pct is not None:
        direction = 1 if req.opt_type == "CE" else -1
        stretched_5d = m.stretch_5d_pct is not None and direction * m.stretch_5d_pct >= cfg.chasing_5d_pct
        stretched_20dma = m.stretch_20dma_pct is not None and direction * m.stretch_20dma_pct >= cfg.chasing_20dma_pct
        if stretched_5d or stretched_20dma:
            flags.append(Flag(
                "CHASING", "amber",
                "The underlying has already moved sharply in this trade's direction recently.",
            ))

    if m.hv20 and m.iv is not None:
        iv_hv_ratio = m.iv / m.hv20
        if iv_hv_ratio > cfg.iv_hv_amber or (m.hv_percentile is not None and m.hv_percentile > cfg.hv_pct_amber):
            flags.append(Flag(
                "EXPENSIVE_VOL", "amber",
                f"IV/HV ratio {iv_hv_ratio:.2f}, or current HV20 sits in a high percentile of its own history.",
            ))

    if m.spread_pct is not None:
        if m.spread_pct > cfg.spread_red:
            flags.append(Flag("WIDE_SPREAD", "red", f"Bid/ask spread is {m.spread_pct:.0%} of mid."))
        elif m.spread_pct > cfg.spread_amber:
            flags.append(Flag("WIDE_SPREAD", "amber", f"Bid/ask spread is {m.spread_pct:.0%} of mid."))

    if m.volume < cfg.min_volume or m.oi < cfg.min_oi:
        flags.append(Flag("ILLIQUID", "amber", f"volume={m.volume}, oi={m.oi} -- thin liquidity."))

    if m.earnings_before_expiry:
        flags.append(Flag(
            "EARNINGS_INSIDE", "amber",
            "Earnings fall before expiry -- IV often drops sharply right after results.",
        ))

    if m.trading_days_to_expiry < cfg.near_expiry_days:
        flags.append(Flag(
            "NEAR_EXPIRY", "red",
            f"Only {m.trading_days_to_expiry} trading days to expiry.",
        ))

    if m.iv is None or m.iv_from_ltp:
        flags.append(Flag(
            "IV_UNRELIABLE", "amber",
            "Implied vol could not be derived from a tight bid/ask -- using last traded price instead.",
        ))

    if m.delta is not None and abs(m.delta) < 0.05:
        flags.append(Flag(
            "DELTA_TOO_SMALL", "amber",
            f"Delta is only {m.delta:.2f} -- this barely moves with the underlying at all.",
        ))

    if oi_context:
        if req.opt_type == "CE" and oi_context.get("max_call_oi_strike") is not None:
            if req.strike > oi_context["max_call_oi_strike"]:
                flags.append(Flag("OI_WALL", "amber", "Strike sits above the max call-OI strike (context only)."))
        if req.opt_type == "PE" and oi_context.get("max_put_oi_strike") is not None:
            if req.strike < oi_context["max_put_oi_strike"]:
                flags.append(Flag("OI_WALL", "amber", "Strike sits below the max put-OI strike (context only)."))

    if max_lots == 0:
        flags.append(Flag("TOO_BIG_FOR_CAP", "red", "max_loss_per_trade can't fund even one lot at the current stop."))

    return flags


def overall_verdict(flags: list[Flag]) -> str:
    if any(f.level == "red" for f in flags):
        return "red"
    if any(f.level == "amber" for f in flags):
        return "amber"
    return "green"
