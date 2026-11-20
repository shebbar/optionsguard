"""Section 7.9.4: Monte Carlo target/stop/time-exit odds -- deterministic
for a fixed seed, the three outcomes sum to 1, and moving the target
farther away lowers the target-first probability."""
from __future__ import annotations

import pytest

from optionguard.pricing.mc import simulate_target_stop_first

BASE_KWARGS = dict(
    spot=2500.0, strike=2500.0, opt_type="CE", r=0.065, iv=0.30, time_to_expiry_entry=30 / 252,
    duration_days=5, steps_per_day=10, n_paths=5000, seed=7, trading_days_per_year=252,
)


def test_deterministic_for_a_fixed_seed():
    r1 = simulate_target_stop_first(target_premium=120, stop_premium=60, **BASE_KWARGS)
    r2 = simulate_target_stop_first(target_premium=120, stop_premium=60, **BASE_KWARGS)
    assert r1 == r2


def test_outcomes_sum_to_one():
    r = simulate_target_stop_first(target_premium=120, stop_premium=60, **BASE_KWARGS)
    assert r["target_first"] + r["stop_first"] + r["neither"] == pytest.approx(1.0)
    assert 0.0 <= r["target_first"] <= 1.0
    assert 0.0 <= r["stop_first"] <= 1.0
    assert 0.0 <= r["neither"] <= 1.0


def test_moving_target_farther_lowers_target_first_probability():
    near = simulate_target_stop_first(target_premium=110, stop_premium=60, **BASE_KWARGS)
    far = simulate_target_stop_first(target_premium=300, stop_premium=60, **BASE_KWARGS)
    assert far["target_first"] < near["target_first"]


def test_rejects_duration_running_past_expiry():
    with pytest.raises(ValueError, match="duration runs past"):
        simulate_target_stop_first(
            target_premium=120, stop_premium=60,
            **{**BASE_KWARGS, "time_to_expiry_entry": 3 / 252, "duration_days": 5},
        )
