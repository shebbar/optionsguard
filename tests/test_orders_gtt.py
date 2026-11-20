"""Section 7.8.5 / 7.8.9: GTT trigger/limit calculation -- pure functions."""
from __future__ import annotations

from optionguard.orders.gtt import build_gtt, gtt_stop_limit, round_to_tick


def test_round_to_tick_snaps_to_nearest_nickel():
    assert round_to_tick(100.03) == 100.05
    assert round_to_tick(100.02) == 100.0
    assert round_to_tick(100.00) == 100.0


def test_gtt_stop_limit_applies_buffer_and_tick_rounds():
    # 100 * (1 - 0.10) = 90.0 exactly -- already tick-aligned.
    assert gtt_stop_limit(100.0, 0.10) == 90.0
    # 283.43 * 0.9 = 255.087 -> nearest 0.05 is 255.10.
    assert gtt_stop_limit(283.43, 0.10) == round_to_tick(283.43 * 0.9)


def test_build_gtt_single_trigger_when_no_target():
    spec = build_gtt(quantity=175, stop_trigger=150.0, buffer_pct=0.10)
    assert spec["trigger_values"] == [150.0]
    assert len(spec["orders"]) == 1
    leg = spec["orders"][0]
    assert leg["transaction_type"] == "SELL"
    assert leg["quantity"] == 175
    assert leg["order_type"] == "LIMIT"
    assert leg["product"] == "NRML"
    assert leg["price"] == gtt_stop_limit(150.0, 0.10)


def test_build_gtt_oco_when_target_set():
    spec = build_gtt(quantity=175, stop_trigger=150.0, buffer_pct=0.10, target_trigger=250.0)
    assert spec["trigger_values"] == [150.0, 250.0]  # ascending, stop first
    assert len(spec["orders"]) == 2
    stop_leg, target_leg = spec["orders"]
    assert stop_leg["price"] == gtt_stop_limit(150.0, 0.10)  # buffered
    assert target_leg["price"] == 250.0  # sold at the target itself, no discount
    assert stop_leg["transaction_type"] == target_leg["transaction_type"] == "SELL"
