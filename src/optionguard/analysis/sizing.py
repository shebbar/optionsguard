"""Position sizing from risk rules. Pure function. Per DESIGN.md section 7.3."""
from __future__ import annotations

import math


def max_lots(
    premium: float, lot_size: int, *, max_loss_per_trade: float, premium_stop_pct: float
) -> int:
    if premium <= 0 or lot_size <= 0:
        return 0
    loss_per_lot_at_stop = premium * lot_size * premium_stop_pct
    full_loss_per_lot = premium * lot_size
    if loss_per_lot_at_stop <= 0 or full_loss_per_lot <= 0:
        return 0
    by_stop = math.floor(max_loss_per_trade / loss_per_lot_at_stop)
    by_gap = math.floor(2 * max_loss_per_trade / full_loss_per_lot)
    return max(0, min(by_stop, by_gap))
