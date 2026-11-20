"""Section 7.9.8: the scorecard's pure aggregation functions, against
directly-crafted OptionThesis/OptionThesisScore/OptionThesisPolicyResult
rows (bypassing the sync pipeline entirely -- these numbers are hand-
computed, not derived from a random mock market path), mirroring
test_theses_scorecard.py's pattern for section 7.7."""
from __future__ import annotations

from datetime import date

from optionguard.db import repo
from optionguard.db.models import Check, OptionThesis, OptionThesisScore
from optionguard.option_theses import scorecard as sc

D0 = date(2026, 1, 1)


def _check_row(db, offset: int) -> Check:
    row = Check(
        created_at=repo.utcnow(), batch_id="scorecard-test", symbol="TCS", expiry=date(2026, 12, 31),
        strike=2500 + offset, opt_type="CE", data_source="kite", spot=2500, premium=50, lot_size=1,
        verdict="green", flags=[], max_lots=5, raw={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _thesis_row(db, *, offset: int, outcome: str, verdict="green", sector="IT", override_id=None, mkt_prob_target_first=0.25) -> OptionThesis:
    check = _check_row(db, offset)
    thesis = OptionThesis(
        created_at=repo.utcnow(), status="scored", symbol="TCS", expiry=date(2026, 12, 31), strike=2500 + offset,
        opt_type="CE", opt_instrument_token=None, mode="paper", data_source="kite", sector=sector,
        sector_index=None, why="scorecard test fixture, not a real trade idea", check_id=check.id, verdict=verdict,
        check_snapshot={}, override_id=override_id, lot_size=1, lots=1, entry_premium=50, target_premium=70,
        stop_premium=40, duration_days=5, end_date=date(2026, 1, 10), reward_risk=2.0, spot_for_target=None,
        spot_for_stop=None, mkt_prob_target_first=mkt_prob_target_first, mkt_prob_stop_first=0.5,
        mkt_prob_time_exit=0.25, spot_entry=2500, sector_index_entry=None, market_index_entry=None, entry_flags=[],
        outcome=outcome, exit_time=repo.utcnow(),
        exit_premium={"target": 70, "stop": 40, "time": 55, "manual": 60}[outcome],
    )
    db.add(thesis)
    db.commit()
    db.refresh(thesis)
    return thesis


def _score_row(db, thesis, *, pnl, r_multiple, days_held, target_after_stop=False, mfe_pct=None) -> OptionThesisScore:
    fields = dict(
        scored_at=repo.utcnow(), score_version=1, days_held=days_held, ret_pct=0.1, pnl=pnl, charges=60,
        r_multiple=r_multiple, mfe_pct=mfe_pct, mfe_time=None, mae_pct=None, mae_time=None,
        target_after_stop=target_after_stop, planned_outcome=None, planned_r=None, spot_ret_pct=None,
        sector_ret_pct=None, market_ret_pct=None, bars="minute", flags=[], path=[],
    )
    return repo.upsert_option_thesis_score(db, thesis.id, **fields)


def _policy_row(db, thesis, policy, *, pnl, r_multiple, outcome="target", days_held=3, pending=False):
    return repo.upsert_option_thesis_policy_result(
        db, thesis.id, policy, policy_params={}, outcome=None if pending else outcome, exit_time=None,
        exit_premium=None, days_held=None if pending else days_held, pnl=None if pending else pnl,
        r_multiple=None if pending else r_multiple, pending=pending, scored_at=repo.utcnow(),
    )


def _three_theses(db):
    t1 = _thesis_row(db, offset=1, outcome="target")
    _score_row(db, t1, pnl=1000, r_multiple=2.0, days_held=3, mfe_pct=0.3)
    t2 = _thesis_row(db, offset=2, outcome="stop")
    _score_row(db, t2, pnl=-500, r_multiple=-1.0, days_held=2, target_after_stop=True, mfe_pct=0.1)
    t3 = _thesis_row(db, offset=3, outcome="stop")
    _score_row(db, t3, pnl=-500, r_multiple=-1.0, days_held=1, target_after_stop=False, mfe_pct=0.05)
    return t1, t2, t3


def test_group_stats_matches_hand_computed_numbers(db):
    t1, t2, t3 = _three_theses(db)
    pairs = repo.list_scored_option_theses_with_scores(db)
    assert {t.id for t, _ in pairs} == {t1.id, t2.id, t3.id}

    stats = sc.group_stats(pairs, n_cancelled=0)
    assert stats["n"] == 3
    assert stats["win_rate"] == pytest_approx(1 / 3)
    assert stats["target_rate"] == pytest_approx(1 / 3)
    assert stats["avg_win"] == 1000
    assert stats["avg_loss"] == -500
    assert stats["payoff_ratio"] == 2.0
    assert stats["expectancy"] == 0.0
    assert stats["expectancy_r"] == 0.0
    assert stats["largest_loss"] == -500
    assert stats["largest_loss_share"] == pytest_approx(-0.5)
    assert stats["stop_rate"] == pytest_approx(2 / 3)
    assert stats["median_days_held"] == 2
    assert stats["mean_mkt_prob_target_first"] == pytest_approx(0.25)
    assert stats["target_after_stop_rate"] == pytest_approx(0.5)
    assert stats["median_mfe_pct_stopped_or_time"] == pytest_approx(0.075)


def pytest_approx(x):
    import pytest

    return pytest.approx(x)


def test_policy_block_pairwise_vs_plain(db):
    t1, t2, t3 = _three_theses(db)
    pairs = repo.list_scored_option_theses_with_scores(db)

    _policy_row(db, t1, "plain", pnl=1000, r_multiple=2.0, outcome="target")
    _policy_row(db, t2, "plain", pnl=-500, r_multiple=-1.0, outcome="stop")
    _policy_row(db, t3, "plain", pnl=-500, r_multiple=-1.0, outcome="stop")

    _policy_row(db, t1, "grace3", pnl=1000, r_multiple=2.0, outcome="target")
    _policy_row(db, t2, "grace3", pnl=-300, r_multiple=-0.6, outcome="disaster")
    _policy_row(db, t3, "grace3", pnl=-600, r_multiple=-1.2, outcome="disaster")

    _policy_row(db, t1, "hold", pnl=0, r_multiple=0, outcome="hold_end", pending=True)
    _policy_row(db, t2, "hold", pnl=0, r_multiple=0, outcome="hold_end", pending=True)
    _policy_row(db, t3, "hold", pnl=0, r_multiple=0, outcome="hold_end", pending=True)

    thesis_ids = [t.id for t, _ in pairs]
    rows = repo.list_option_thesis_policy_results_for_theses(db, thesis_ids)
    by_thesis: dict[int, dict] = {}
    for r in rows:
        by_thesis.setdefault(r.thesis_id, {})[r.policy] = r

    block = sc.policy_block(pairs, by_thesis)
    assert block["plain"]["n"] == 3
    assert block["grace3"]["n"] == 3
    assert block["grace3"]["vs_plain"]["better"] == 1
    assert block["grace3"]["vs_plain"]["worse"] == 1
    assert block["grace3"]["vs_plain"]["pnl_diff_total"] == pytest_approx(100)
    # hold is pending for every thesis -- excluded from its own numbers entirely.
    assert block["hold"]["n"] == 0


def test_independence_note_counts_distinct_entry_dates_and_sectors(db):
    t1, t2, t3 = _three_theses(db)
    pairs = repo.list_scored_option_theses_with_scores(db)
    note = sc.independence_note(pairs)
    assert note["n_theses"] == 3
    # all three fixtures share created_at=utcnow() and sector="IT" -- one observation.
    assert note["n_distinct_entry_dates"] == 1
    assert note["n_distinct_entry_date_sector_pairs"] == 1


def test_findings_suppressed_below_min_n_then_shown_above_it(db):
    _three_theses(db)
    pairs = repo.list_scored_option_theses_with_scores(db)
    stats = sc.group_stats(pairs, n_cancelled=0)

    suppressed = sc.findings(pairs, stats, {}, findings_min_n=10)
    assert suppressed == ["Not enough scored theses yet (n=3; need 10)."]

    shown = sc.findings(pairs, stats, {}, findings_min_n=2)
    assert any("Expectancy" in f for f in shown)
    assert any("market-implied" in f for f in shown)
    assert any("Largest loss equals" in f for f in shown)
    assert any("stops may be too tight" in f for f in shown)


def test_mock_and_cancelled_theses_excluded_but_cancelled_is_counted(db):
    mock_thesis = _thesis_row(db, offset=4, outcome="target")
    mock_thesis.data_source = "mock"
    db.commit()
    _score_row(db, mock_thesis, pnl=500, r_multiple=1.0, days_held=1)

    cancelled = _thesis_row(db, offset=5, outcome="target")
    repo.update_option_thesis(
        db, cancelled.id, status="cancelled", cancelled_at=repo.utcnow(),
        cancel_reason="no trade linked by the end of its entry day",
    )

    cancelled_count = repo.count_cancelled_option_theses(db)
    assert cancelled_count == 1

    pairs = repo.list_scored_option_theses_with_scores(db)
    assert mock_thesis.id not in [t.id for t, _ in pairs]
    assert cancelled.id not in [t.id for t, _ in pairs]
    stats = sc.group_stats(pairs, n_cancelled=cancelled_count)
    assert stats["n"] == 0
    assert stats["n_cancelled"] == 1
