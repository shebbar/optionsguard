"""Section 7.9.8: GET /option-theses/scorecard. Aggregates OptionThesisScore
rows plus the thesis's own outcome/verdict/sector fields and the 7.9.7
policy comparison. Pure functions, no I/O -- the caller (api/option_theses.py,
task #65) fetches the rows. Excludes mock, unresolvable and cancelled
theses; those are counted separately, mirroring theses/scorecard.py's
precedent for section 7.7."""
from __future__ import annotations

import statistics

from optionguard.db.models import OptionThesis, OptionThesisPolicyResult, OptionThesisScore
from optionguard.option_theses.policies import POLICY_NAMES

GROUP_BY_OPTIONS = {"sector", "verdict", "outcome", "symbol", "opt_type", "override", "month", "none"}


def group_key(thesis: OptionThesis, group_by: str) -> str:
    if group_by == "sector":
        return thesis.sector
    if group_by == "verdict":
        return thesis.verdict
    if group_by == "outcome":
        return thesis.outcome or "unresolvable"
    if group_by == "symbol":
        return thesis.symbol
    if group_by == "opt_type":
        return thesis.opt_type
    if group_by == "override":
        return "overridden" if thesis.override_id is not None else "not_overridden"
    if group_by == "month":
        return thesis.created_at.strftime("%Y-%m")
    return "all"


def _mean(values) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _median(values) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return float(statistics.median(vals)) if vals else None


def _ratio(num: int, den: int) -> float | None:
    return (num / den) if den else None


def group_stats(pairs: list[tuple[OptionThesis, OptionThesisScore]], n_cancelled: int) -> dict:
    n = len(pairs)
    pnls = [float(s.pnl) for _, s in pairs]
    rs = [float(s.r_multiple) for _, s in pairs]
    wins = [float(s.pnl) for _, s in pairs if float(s.pnl) > 0]
    losses = [float(s.pnl) for _, s in pairs if float(s.pnl) <= 0]
    win_rs = [float(s.r_multiple) for _, s in pairs if float(s.pnl) > 0]
    loss_rs = [float(s.r_multiple) for _, s in pairs if float(s.pnl) <= 0]

    avg_win, avg_loss = _mean(wins), _mean(losses)
    sum_wins = sum(wins) if wins else 0.0
    largest_loss = min(losses) if losses else None

    targets = sum(1 for t, _ in pairs if t.outcome == "target")
    stops = sum(1 for t, _ in pairs if t.outcome == "stop")
    times = sum(1 for t, _ in pairs if t.outcome == "time")
    manuals = sum(1 for t, _ in pairs if t.outcome == "manual")

    stop_or_time_scores = [s for t, s in pairs if t.outcome in ("stop", "time")]
    stop_n = stops
    target_after_stop_n = sum(1 for t, s in pairs if t.outcome == "stop" and s.target_after_stop)

    return {
        "n": n,
        "n_cancelled": n_cancelled,
        "win_rate": _ratio(len(wins), n),
        "target_rate": _ratio(targets, n),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_win_r": _mean(win_rs),
        "avg_loss_r": _mean(loss_rs),
        "payoff_ratio": (avg_win / abs(avg_loss)) if avg_win is not None and avg_loss not in (None, 0) else None,
        "expectancy": _mean(pnls),
        "expectancy_r": _mean(rs),
        "largest_loss": largest_loss,
        "largest_loss_share": (largest_loss / sum_wins) if largest_loss is not None and sum_wins else None,
        "stop_rate": _ratio(stops, n),
        "time_exit_rate": _ratio(times, n),
        "manual_rate": _ratio(manuals, n),
        "median_days_held": _median([s.days_held for _, s in pairs]),
        "mean_mkt_prob_target_first": _mean(
            [float(t.mkt_prob_target_first) if t.mkt_prob_target_first is not None else None for t, _ in pairs]
        ),
        "target_after_stop_rate": _ratio(target_after_stop_n, stop_n),
        "median_mfe_pct_stopped_or_time": _median([s.mfe_pct for s in stop_or_time_scores]),
    }


def policy_block(
    pairs: list[tuple[OptionThesis, OptionThesisScore]],
    policy_results: dict[int, dict[str, OptionThesisPolicyResult]],
) -> dict:
    """7.9.7's comparison table, one entry per policy, over the same set of
    theses as `pairs`. A policy row that's still `pending`, or has no
    outcome (`unresolvable`), is left out of that policy's own numbers."""
    out: dict[str, dict] = {}
    for policy in POLICY_NAMES:
        rows = [
            r for t, _ in pairs
            if (r := policy_results.get(t.id, {}).get(policy)) is not None
            and not r.pending and r.outcome not in (None, "unresolvable")
        ]
        n = len(rows)
        pnls = [float(r.pnl) for r in rows if r.pnl is not None]
        rs = [float(r.r_multiple) for r in rows if r.r_multiple is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        avg_win, avg_loss = _mean(wins), _mean(losses)
        sum_wins = sum(wins) if wins else 0.0
        largest_loss = min(losses) if losses else None
        outcome_mix: dict[str, int] = {}
        for r in rows:
            outcome_mix[r.outcome] = outcome_mix.get(r.outcome, 0) + 1

        entry = {
            "n": n,
            "win_rate": _ratio(len(wins), n),
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "expectancy": _mean(pnls),
            "expectancy_r": _mean(rs),
            "largest_loss": largest_loss,
            "largest_loss_share": (largest_loss / sum_wins) if largest_loss is not None and sum_wins else None,
            "median_days_held": _median([r.days_held for r in rows]),
            "outcome_mix": outcome_mix,
        }
        if policy != "plain":
            better = worse = 0
            diff_total = 0.0
            for t, _ in pairs:
                plain_r = policy_results.get(t.id, {}).get("plain")
                this_r = policy_results.get(t.id, {}).get(policy)
                if (
                    plain_r is None or this_r is None or plain_r.pending or this_r.pending
                    or plain_r.pnl is None or this_r.pnl is None
                ):
                    continue
                diff = float(this_r.pnl) - float(plain_r.pnl)
                diff_total += diff
                if diff > 0:
                    better += 1
                elif diff < 0:
                    worse += 1
            entry["vs_plain"] = {"better": better, "worse": worse, "pnl_diff_total": diff_total}
        out[policy] = entry
    return out


def independence_note(pairs: list[tuple[OptionThesis, OptionThesisScore]]) -> dict:
    """7.9.8: five IT theses opened on one day count as roughly one
    observation of the market."""
    entry_dates = {t.created_at.date() for t, _ in pairs}
    entry_date_sector_pairs = {(t.created_at.date(), t.sector) for t, _ in pairs}
    return {
        "n_theses": len(pairs),
        "n_distinct_entry_dates": len(entry_dates),
        "n_distinct_entry_date_sector_pairs": len(entry_date_sector_pairs),
    }


def findings(
    pairs: list[tuple[OptionThesis, OptionThesisScore]], stats: dict, policy_stats: dict, findings_min_n: int,
) -> list[str]:
    n = len(pairs)
    if n < findings_min_n:
        return [f"Not enough scored theses yet (n={n}; need {findings_min_n})."]

    out: list[str] = []
    exp, exp_r = stats.get("expectancy"), stats.get("expectancy_r")
    if exp is not None and exp_r is not None:
        out.append(f"Expectancy {exp:+,.0f} per thesis ({exp_r:+.2f}R) over {n} theses.")

    target_rate, mkt_rate = stats.get("target_rate"), stats.get("mean_mkt_prob_target_first")
    if target_rate is not None and mkt_rate is not None:
        out.append(f"Targets hit {target_rate:.0%} vs {mkt_rate:.0%} market-implied.")

    if stats.get("largest_loss_share") is not None:
        out.append(f"Largest loss equals {stats['largest_loss_share']:.0%} of all wins.")

    stop_n = sum(1 for t, _ in pairs if t.outcome == "stop")
    target_after_stop_rate = stats.get("target_after_stop_rate")
    if target_after_stop_rate is not None and stop_n:
        hit_n = round(target_after_stop_rate * stop_n)
        out.append(f"Stopped theses later reached target in {hit_n} of {stop_n}: stops may be too tight.")

    for policy, pstats in policy_stats.items():
        if policy == "plain" or pstats["n"] < findings_min_n:
            continue
        pexp, plain_exp = pstats.get("expectancy"), policy_stats.get("plain", {}).get("expectancy")
        vs_plain = pstats.get("vs_plain")
        if pexp is not None and plain_exp is not None and vs_plain is not None:
            out.append(
                f"Policy {policy}: expectancy {pexp:+,.0f} vs plain {plain_exp:+,.0f}; "
                f"better on {vs_plain['better']} theses, worse on {vs_plain['worse']} "
                f"(net {vs_plain['pnl_diff_total']:+,.0f})."
            )
        elif pexp is not None and pstats.get("largest_loss_share") is not None:
            out.append(
                f"Policy {policy}: expectancy {pexp:+,.0f}; "
                f"largest loss equals {pstats['largest_loss_share']:.0%} of all wins."
            )
    return out
