"""OI context -- descriptive only, never a signal. Per DESIGN.md section 7.5.
Kite/mock mode only; manual mode returns None."""
from __future__ import annotations

from datetime import date

from optionguard.market.base import MarketProvider, NotAvailable

MAX_STRIKES = 200  # keeps the single chain() quote call well under 500 instruments (x2 for CE/PE)


def compute_oi_context(provider: MarketProvider, symbol: str, expiry: date, spot: float) -> dict | None:
    try:
        all_strikes = provider.list_strikes(symbol, expiry)
    except NotAvailable:
        return None

    lo, hi = spot * 0.9, spot * 1.1
    strikes = [s for s in all_strikes if lo <= s <= hi][:MAX_STRIKES]
    if not strikes:
        return None

    try:
        quotes = provider.chain(symbol, expiry, strikes)
    except NotAvailable:
        return None
    if not quotes:
        return None

    calls = [q for q in quotes if q.opt_type == "CE"]
    puts = [q for q in quotes if q.opt_type == "PE"]
    max_call = max(calls, key=lambda q: q.oi, default=None)
    max_put = max(puts, key=lambda q: q.oi, default=None)
    total_call_oi = sum(q.oi for q in calls)
    total_put_oi = sum(q.oi for q in puts)

    def top3(qs: list) -> list[dict]:
        return [{"strike": q.strike, "oi": q.oi} for q in sorted(qs, key=lambda q: q.oi, reverse=True)[:3]]

    return {
        "max_call_oi_strike": max_call.strike if max_call else None,
        "max_put_oi_strike": max_put.strike if max_put else None,
        "pcr_oi": (total_put_oi / total_call_oi) if total_call_oi else None,
        "top_call_strikes": top3(calls),
        "top_put_strikes": top3(puts),
        "note": "Context only, not a signal.",
    }
