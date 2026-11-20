"""Section 7.8.5: GTT trigger/limit calculation. Pure functions, no I/O.

Both legs are always SELL (closing a long option position) -- stop below
entry, target (if set) above it -- regardless of CE/PE, since this watches
the option's own premium, not the underlying's direction."""
from __future__ import annotations

TICK_SIZE = 0.05


def round_to_tick(price: float, tick_size: float = TICK_SIZE) -> float:
    return round(round(price / tick_size) * tick_size, 2)


def gtt_stop_limit(stop_trigger: float, buffer_pct: float) -> float:
    """Sell limit = trigger x (1 - buffer); a worse fill traded for a
    better chance of actually getting out on a gap (7.3's sizing rule
    already assumes a gap like this can happen)."""
    return round_to_tick(stop_trigger * (1 - buffer_pct))


def build_gtt(*, quantity: int, stop_trigger: float, buffer_pct: float, target_trigger: float | None = None) -> dict:
    """Returns {"trigger_values": [...], "orders": [...]} ready for
    KiteProvider.place_gtt/modify_gtt -- single (stop only) or two-leg OCO
    (stop + target). trigger_values stays ascending and index-aligned with
    orders, matching how a GTT reports which leg fired."""
    stop_leg = {
        "transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT",
        "product": "NRML", "price": gtt_stop_limit(stop_trigger, buffer_pct),
    }
    if target_trigger is None:
        return {"trigger_values": [round_to_tick(stop_trigger)], "orders": [stop_leg]}

    target_leg = {
        "transaction_type": "SELL", "quantity": quantity, "order_type": "LIMIT",
        "product": "NRML", "price": round_to_tick(target_trigger),
    }
    return {
        "trigger_values": [round_to_tick(stop_trigger), round_to_tick(target_trigger)],
        "orders": [stop_leg, target_leg],
    }
