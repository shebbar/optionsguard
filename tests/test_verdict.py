from __future__ import annotations

from datetime import date

from optionguard.analysis.metrics import CandidateRequest, Metrics
from optionguard.analysis.verdict import compute_flags, overall_verdict
from optionguard.config import VerdictConfig

CFG = VerdictConfig()
REQ = CandidateRequest(symbol="TCS", expiry=date(2026, 11, 26), strike=2500, opt_type="CE", data_source="manual")


def _metrics(**overrides) -> Metrics:
    base = dict(
        spot=2480, premium=35, bid=None, ask=None, lot_size=175, iv=0.25, iv_from_ltp=False,
        hv20=0.22, hv60=0.20, hv_percentile=50, breakeven=2535, breakeven_pct=0.02,
        expected_move_pct=0.1, be_to_move_ratio=0.2, prob_profit=0.5, prob_zero=0.4, prob_thesis=None,
        theta_per_day_lot=-100, theta_final_2w_lot=-200, delta=0.5, daily_move_needed=2.0,
        daily_move_needed_pct=0.0008, stretch_5d_pct=0.0, stretch_20dma_pct=0.0,
        spread_pct=0.03, volume=1000, oi=5000, earnings_before_expiry=False, trading_days_to_expiry=20,
    )
    base.update(overrides)
    return Metrics(**base)


def test_low_prob_red_at_threshold():
    flags = compute_flags(_metrics(prob_profit=CFG.low_prob_red - 0.01), REQ, CFG, max_lots=1)
    assert any(f.code == "LOW_PROB" and f.level == "red" for f in flags)


def test_low_prob_soft_amber_band():
    flags = compute_flags(_metrics(prob_profit=(CFG.low_prob_red + CFG.low_prob_amber) / 2), REQ, CFG, max_lots=1)
    assert any(f.code == "LOW_PROB_SOFT" and f.level == "amber" for f in flags)


def test_far_breakeven_red():
    flags = compute_flags(_metrics(be_to_move_ratio=CFG.be_to_move_red + 0.1), REQ, CFG, max_lots=1)
    assert any(f.code == "FAR_BREAKEVEN" and f.level == "red" for f in flags)


def test_chasing_amber_for_call_in_direction():
    flags = compute_flags(_metrics(stretch_5d_pct=CFG.chasing_5d_pct + 0.01), REQ, CFG, max_lots=1)
    assert any(f.code == "CHASING" for f in flags)


def test_chasing_not_triggered_opposite_direction():
    flags = compute_flags(_metrics(stretch_5d_pct=-(CFG.chasing_5d_pct + 0.05)), REQ, CFG, max_lots=1)
    assert not any(f.code == "CHASING" for f in flags)


def test_expensive_vol_amber():
    flags = compute_flags(_metrics(iv=0.30, hv20=0.30 / (CFG.iv_hv_amber + 0.1)), REQ, CFG, max_lots=1)
    assert any(f.code == "EXPENSIVE_VOL" for f in flags)


def test_wide_spread_red_vs_amber():
    red_flags = compute_flags(_metrics(spread_pct=CFG.spread_red + 0.01), REQ, CFG, max_lots=1)
    amber_flags = compute_flags(_metrics(spread_pct=CFG.spread_amber + 0.005), REQ, CFG, max_lots=1)
    assert any(f.code == "WIDE_SPREAD" and f.level == "red" for f in red_flags)
    assert any(f.code == "WIDE_SPREAD" and f.level == "amber" for f in amber_flags)


def test_illiquid_amber():
    flags = compute_flags(_metrics(volume=CFG.min_volume - 1), REQ, CFG, max_lots=1)
    assert any(f.code == "ILLIQUID" for f in flags)


def test_earnings_inside_amber():
    flags = compute_flags(_metrics(earnings_before_expiry=True), REQ, CFG, max_lots=1)
    assert any(f.code == "EARNINGS_INSIDE" for f in flags)


def test_near_expiry_red():
    flags = compute_flags(_metrics(trading_days_to_expiry=CFG.near_expiry_days - 1), REQ, CFG, max_lots=1)
    assert any(f.code == "NEAR_EXPIRY" and f.level == "red" for f in flags)


def test_iv_unreliable_amber():
    flags = compute_flags(_metrics(iv=None), REQ, CFG, max_lots=1)
    assert any(f.code == "IV_UNRELIABLE" for f in flags)


def test_delta_too_small_amber():
    flags = compute_flags(_metrics(delta=0.03, daily_move_needed=None, daily_move_needed_pct=None), REQ, CFG, max_lots=1)
    assert any(f.code == "DELTA_TOO_SMALL" and f.level == "amber" for f in flags)


def test_delta_too_small_not_triggered_for_healthy_delta():
    flags = compute_flags(_metrics(delta=0.5), REQ, CFG, max_lots=1)
    assert not any(f.code == "DELTA_TOO_SMALL" for f in flags)


def test_too_big_for_cap_red():
    flags = compute_flags(_metrics(), REQ, CFG, max_lots=0)
    assert any(f.code == "TOO_BIG_FOR_CAP" and f.level == "red" for f in flags)


def test_red_dominates_amber_in_overall_verdict():
    flags = compute_flags(_metrics(volume=1, prob_profit=0.1), REQ, CFG, max_lots=1)
    codes = {f.level for f in flags}
    assert "red" in codes and "amber" in codes
    assert overall_verdict(flags) == "red"


def test_clean_candidate_is_green():
    flags = compute_flags(_metrics(), REQ, CFG, max_lots=2)
    assert overall_verdict(flags) == "green"
