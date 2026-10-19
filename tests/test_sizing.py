from __future__ import annotations

from optionguard.analysis.sizing import max_lots


def test_max_lots_normal_case():
    # loss_per_lot_at_stop = 20*175*0.4 = 1400; by_stop = floor(5000/1400) = 3
    # full_loss_per_lot = 20*175 = 3500; by_gap = floor(2*5000/3500) = 2
    assert max_lots(20, 175, max_loss_per_trade=5000, premium_stop_pct=0.4) == 2


def test_max_lots_zero_when_too_expensive():
    assert max_lots(500, 1500, max_loss_per_trade=5000, premium_stop_pct=0.4) == 0


def test_max_lots_zero_premium():
    assert max_lots(0, 175, max_loss_per_trade=5000, premium_stop_pct=0.4) == 0


def test_max_lots_gap_protection_binds():
    # by_stop would allow many lots at a tiny premium_stop_pct, but the 2x
    # full-loss gap-protection cap should bind instead.
    lots = max_lots(10, 100, max_loss_per_trade=1000, premium_stop_pct=0.01)
    full_loss_per_lot = 10 * 100
    assert lots == 1000 * 2 // full_loss_per_lot


def test_max_lots_negative_inputs_safe():
    assert max_lots(-5, 175, max_loss_per_trade=5000, premium_stop_pct=0.4) == 0
