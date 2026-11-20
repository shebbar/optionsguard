"""Section 7.9.6: the Resolve & score button, folded into theses/sync.py's
run_sync() as new steps (same advisory lock, same sync_runs row -- this
module does no locking of its own). Order matters: bars before resolve
(resolve needs them), resolve before score (score needs the outcome),
auto-cancel before resolve (an auto-cancelled thesis should not also be
resolved this run), score before policies (policies are computed only for
'scored' theses)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from optionguard.config import Config
from optionguard.db import repo
from optionguard.db.models import Trade
from optionguard.market.base import MarketProvider, NotAvailable
from optionguard.market.factory import get_kite_provider, get_mock_kite_provider
from optionguard.option_theses import policies as policy_rules
from optionguard.option_theses.resolve import Bar, is_late_manual_exit, resolve_paper
from optionguard.option_theses.scoring import score_thesis
from optionguard.pricing.calendar import IST, now_ist, trading_day_on_or_before, trading_days_between

# Trade.exit_reason -> OptionThesis.outcome (7.9.5); anything not listed (thesis_stop, manual, expiry) -> "manual".
EXIT_REASON_TO_OUTCOME = {"target": "target", "premium_stop": "stop", "time_stop": "time"}
SCORE_READY_TIME = time(15, 45)


def _provider_for(data_source: str) -> MarketProvider:
    return get_mock_kite_provider() if data_source == "mock" else get_kite_provider()


def _parse_ts(raw) -> datetime:
    return raw if isinstance(raw, datetime) else datetime.fromisoformat(raw)


def _ist_close(d: date) -> datetime:
    return datetime.combine(d, time(15, 30), tzinfo=IST)


def _first_full_minute_after(ts: datetime) -> datetime:
    boundary = ts.replace(second=0, microsecond=0)
    return boundary + timedelta(minutes=1) if boundary <= ts else boundary


def _hold_cutoff_date(thesis, holidays: set[date], cfg: Config) -> date:
    target = thesis.expiry - timedelta(days=cfg.option_theses.hold_cutoff_calendar_days)
    return trading_day_on_or_before(target, holidays)


def _minute_bars(db: Session, thesis, from_ts: datetime, to_ts: datetime) -> list[Bar]:
    rows = repo.list_option_minute_bars(db, thesis.opt_instrument_token, from_ts=from_ts, to_ts=to_ts)
    return [Bar(ts=r.ts, o=float(r.o), h=float(r.h), l=float(r.l), c=float(r.c)) for r in rows if r.o is not None]


def _daily_fallback_bars(thesis, provider: MarketProvider, start: date, end: date) -> list[Bar]:
    """7.9.5's missing-data fallback: daily candles when minute data is
    incomplete because the contract expired before it could be fetched.
    resolve_paper's own stop-wins-on-a-tie rule already gives "same-day
    target and stop counted as stop-first" for free."""
    raw = provider.client.historical_data(thesis.opt_instrument_token, start, end, "day")
    return [
        Bar(ts=_parse_ts(b["date"]), o=float(b["open"]), h=float(b["high"]), l=float(b["low"]), c=float(b["close"]))
        for b in raw
    ]


def _underlying_close_at(provider: MarketProvider, symbol: str, d: date) -> float | None:
    try:
        bars = provider.history(symbol, days=400)
    except NotAvailable:
        return None
    match = next((b for b in bars if b.day == d), None)
    return match.close if match else None


def _fetch_option_minute_bars(db: Session, cfg: Config, today: date, now: datetime, holidays: set[date]) -> tuple[int, list[dict]]:
    """Step 1 (7.9.6): bars for every non-mock, unexpired thesis (paper and
    real -- 7.9.7's policy replay needs them for real too), from entry to
    the earlier of now or the hold cutoff date, 15:30 IST."""
    upserted = 0
    errors: list[dict] = []
    for thesis in repo.list_option_theses_needing_bars(db, today):
        try:
            window_end = min(now, _ist_close(_hold_cutoff_date(thesis, holidays, cfg)))
            if window_end <= thesis.created_at:
                continue
            provider = _provider_for(thesis.data_source)
            bars = provider.client.historical_data(thesis.opt_instrument_token, thesis.created_at, window_end, "minute")
            for b in bars:
                repo.upsert_option_minute_bar(
                    db, instrument_token=thesis.opt_instrument_token, ts=_parse_ts(b["date"]),
                    o=b.get("open"), h=b.get("high"), l=b.get("low"), c=b.get("close"),
                    volume=b.get("volume"), oi=b.get("oi"),
                )
                upserted += 1
        except Exception as exc:  # noqa: BLE001 -- one thesis's failure must not stop the rest
            errors.append({"thesis_id": thesis.id, "step": "option_bars", "message": str(exc)})
    return upserted, errors


def _auto_cancel_unlinked_real(db: Session, today: date) -> tuple[int, list[dict]]:
    """7.9.5: a real thesis with no linked trade by the end of its entry
    day is auto-cancelled. Runs before resolution so a just-cancelled
    thesis isn't also picked up as "open" this same run."""
    cancelled = 0
    errors: list[dict] = []
    cutoff = datetime.combine(today, time.min, tzinfo=IST)
    for thesis in repo.list_option_theses_real_unlinked(db, cutoff):
        try:
            repo.update_option_thesis(
                db, thesis.id, status="cancelled", cancelled_at=repo.utcnow(),
                cancel_reason="no trade linked by the end of its entry day",
            )
            cancelled += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"thesis_id": thesis.id, "step": "auto_cancel", "message": str(exc)})
    return cancelled, errors


def _resolve_real_thesis(db: Session, thesis) -> None:
    if thesis.trade_id is None:
        return
    trade = db.get(Trade, thesis.trade_id)
    if trade is None or trade.status != "closed":
        return
    outcome = EXIT_REASON_TO_OUTCOME.get(trade.exit_reason, "manual")
    repo.update_option_thesis(
        db, thesis.id, outcome=outcome, exit_time=trade.exit_time,
        exit_premium=float(trade.exit_premium), status="closed",
    )


def _resolve_paper_thesis(db: Session, cfg: Config, thesis, today: date, now: datetime) -> None:
    start_after = _first_full_minute_after(thesis.created_at)
    bars = _minute_bars(db, thesis, start_after, min(now, _ist_close(thesis.end_date)))
    contract_expired = today > thesis.expiry
    boundary_reached = bool(bars) and bars[-1].ts.date() >= thesis.end_date

    if bars and not boundary_reached:
        result = resolve_paper(
            float(thesis.entry_premium), float(thesis.target_premium), float(thesis.stop_premium),
            bars, cfg.option_theses.paper_slippage_pct,
        )
        if result["outcome"] in ("target", "stop"):
            repo.update_option_thesis(
                db, thesis.id, outcome=result["outcome"], exit_time=result["exit_time"],
                exit_premium=result["exit_premium"], status="closed",
            )
            return
        if not contract_expired:
            return  # "time" fallback on a truncated window isn't real yet; wait for more bars

    if not boundary_reached and contract_expired:
        provider = _provider_for(thesis.data_source)
        daily_bars = _daily_fallback_bars(
            thesis, provider, thesis.created_at.date() + timedelta(days=1), thesis.end_date,
        )
        if daily_bars:
            result = resolve_paper(
                float(thesis.entry_premium), float(thesis.target_premium), float(thesis.stop_premium),
                daily_bars, cfg.option_theses.paper_slippage_pct,
            )
            if result["outcome"] != "unresolvable":
                repo.update_option_thesis(
                    db, thesis.id, outcome=result["outcome"], exit_time=result["exit_time"],
                    exit_premium=result["exit_premium"], status="closed",
                )
                return
        repo.update_option_thesis(db, thesis.id, outcome="unresolvable", status="closed")
        return

    if not bars:
        return  # not expired yet, just no bars fetched so far -- try again next sync

    result = resolve_paper(
        float(thesis.entry_premium), float(thesis.target_premium), float(thesis.stop_premium),
        bars, cfg.option_theses.paper_slippage_pct,
    )
    repo.update_option_thesis(
        db, thesis.id, outcome=result["outcome"], exit_time=result["exit_time"],
        exit_premium=result["exit_premium"], status="closed",
    )


def _resolve_open_theses(db: Session, cfg: Config, today: date, now: datetime) -> tuple[int, list[dict]]:
    """Step 2 (7.9.6): paper theses resolve from bars (7.9.5), real theses
    resolve from their linked, closed trade."""
    resolved = 0
    errors: list[dict] = []
    for thesis in repo.list_option_theses_open(db):
        try:
            before = thesis.status
            if thesis.mode == "real":
                _resolve_real_thesis(db, thesis)
            else:
                _resolve_paper_thesis(db, cfg, thesis, today, now)
            db.refresh(thesis)
            if thesis.status != before:
                resolved += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"thesis_id": thesis.id, "step": "resolve", "message": str(exc)})
    return resolved, errors


def _score_one(db: Session, cfg: Config, thesis, holidays: set[date]) -> None:
    start_after = _first_full_minute_after(thesis.created_at)
    bars = _minute_bars(db, thesis, start_after, _ist_close(thesis.end_date))
    used_fallback = False
    if not bars:
        provider = _provider_for(thesis.data_source)
        bars = _daily_fallback_bars(thesis, provider, thesis.created_at.date() + timedelta(days=1), thesis.end_date)
        used_fallback = bool(bars)

    ambiguous_bar = False
    planned_outcome = planned_exit_premium = None
    if thesis.mode == "real":
        bars_quality = "fill"
        if bars:
            planned = resolve_paper(
                float(thesis.entry_premium), float(thesis.target_premium), float(thesis.stop_premium),
                bars, cfg.option_theses.paper_slippage_pct,
            )
            planned_outcome, planned_exit_premium = planned["outcome"], planned["exit_premium"]
    else:
        bars_quality = "daily_fallback" if used_fallback else ("minute" if bars else "none")
        if bars and thesis.exit_time:
            replay_bars = [b for b in bars if b.ts <= thesis.exit_time]
            replayed = resolve_paper(
                float(thesis.entry_premium), float(thesis.target_premium), float(thesis.stop_premium),
                replay_bars, cfg.option_theses.paper_slippage_pct,
            )
            ambiguous_bar = replayed.get("ambiguous_bar", False)

    trade = db.get(Trade, thesis.trade_id) if thesis.trade_id else None
    charges = float(trade.charges) if trade is not None and trade.charges is not None else cfg.defaults.charges_per_order_estimate

    exit_spot = sector_exit = market_exit = None
    if thesis.exit_time is not None:
        provider = _provider_for(thesis.data_source)
        exit_spot = _underlying_close_at(provider, thesis.symbol, thesis.exit_time.date())
        if thesis.sector_index:
            sector_exit = _underlying_close_at(provider, thesis.sector_index, thesis.exit_time.date())
        market_exit = _underlying_close_at(provider, cfg.option_theses.market_benchmark, thesis.exit_time.date())

    extra_flags = list(thesis.entry_flags or [])
    if used_fallback:
        extra_flags.append("daily_fallback")
    if (
        thesis.manual_exit_entered_at is not None and thesis.exit_time is not None
        and is_late_manual_exit(thesis.exit_time, thesis.manual_exit_entered_at, cfg.option_theses.late_exit_flag_days)
    ):
        extra_flags.append("late_entry")

    result = score_thesis(
        entry_premium=float(thesis.entry_premium), target_premium=float(thesis.target_premium),
        stop_premium=float(thesis.stop_premium), lots=thesis.lots, lot_size=thesis.lot_size,
        charges=charges, created_at=thesis.created_at, holidays=holidays,
        outcome=thesis.outcome, exit_time=thesis.exit_time,
        exit_premium=float(thesis.exit_premium) if thesis.exit_premium is not None else None,
        ambiguous_bar=ambiguous_bar, full_window_bars=bars, bars_quality=bars_quality,
        entry_spot=float(thesis.spot_entry), exit_spot=exit_spot,
        entry_sector_index=float(thesis.sector_index_entry) if thesis.sector_index_entry is not None else None,
        exit_sector_index=sector_exit,
        entry_market_index=float(thesis.market_index_entry) if thesis.market_index_entry is not None else None,
        exit_market_index=market_exit,
        extra_flags=extra_flags, planned_outcome=planned_outcome, planned_exit_premium=planned_exit_premium,
        scored_at=repo.utcnow(),
    )
    repo.upsert_option_thesis_score(db, thesis.id, **result)
    repo.update_option_thesis(db, thesis.id, status="scored")


def _score_closed_theses(db: Session, cfg: Config, today: date, now: datetime, holidays: set[date]) -> tuple[int, list[dict]]:
    """Step 3 (7.9.6): every closed thesis whose tracking window has ended,
    or immediately if it ends today and it's after 15:45 IST."""
    scored = 0
    errors: list[dict] = []
    for thesis in repo.list_option_theses_due_for_scoring(db, today):
        if thesis.end_date == today and now.astimezone(IST).time() < SCORE_READY_TIME:
            continue
        try:
            _score_one(db, cfg, thesis, holidays)
            scored += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"thesis_id": thesis.id, "step": "score", "message": str(exc)})
    return scored, errors


def _policy_outcome(
    entry: float, target: float, bars: list[Bar], entry_date: date, holidays: set[date],
    rules: policy_rules.PolicyRules, slippage_pct: float, boundary_date: date, contract_expired: bool,
) -> tuple[str | None, datetime | None, float | None, bool]:
    """Returns (outcome, exit_time, exit_premium, pending). A fallback exit
    computed from bars that don't yet reach `boundary_date` isn't real --
    stays pending until later bars reach it, or becomes unresolvable if the
    contract expires first (7.9.6 step 4, 7.9.7's hold cutoff note)."""
    if not bars:
        return ("unresolvable", None, None, False) if contract_expired else (None, None, None, True)
    result = policy_rules.replay_policy(entry, target, bars, entry_date, holidays, rules, slippage_pct)
    boundary_reached = bars[-1].ts.date() >= boundary_date
    if result["outcome"] == rules.fallback_outcome and not boundary_reached:
        return ("unresolvable", None, None, False) if contract_expired else (None, None, None, True)
    return result["outcome"], result["exit_time"], result["exit_premium"], False


def _compute_policies_for_thesis(db: Session, cfg: Config, thesis, today: date, holidays: set[date]) -> None:
    pol_cfg = cfg.option_theses.policies
    entry_date = thesis.created_at.date()
    hold_cutoff = _hold_cutoff_date(thesis, holidays, cfg)
    contract_expired = today > thesis.expiry
    entry, target, stop = float(thesis.entry_premium), float(thesis.target_premium), float(thesis.stop_premium)
    slippage = cfg.option_theses.paper_slippage_pct

    start_after = _first_full_minute_after(thesis.created_at)
    bars_to_end = _minute_bars(db, thesis, start_after, _ist_close(thesis.end_date))
    bars_to_hold = _minute_bars(db, thesis, start_after, _ist_close(hold_cutoff))

    trade = db.get(Trade, thesis.trade_id) if thesis.trade_id else None
    charges = float(trade.charges) if trade is not None and trade.charges is not None else cfg.defaults.charges_per_order_estimate

    jobs = [
        ("plain", policy_rules.plain_rules(stop), {}, bars_to_end, thesis.end_date),
        (
            "grace3", policy_rules.grace3_rules(entry, stop, pol_cfg.grace_days, pol_cfg.disaster_stop_pct),
            {"grace_days": pol_cfg.grace_days, "disaster_stop_pct": pol_cfg.disaster_stop_pct},
            bars_to_end, thesis.end_date,
        ),
        (
            "hold", policy_rules.hold_rules(), {"hold_cutoff_calendar_days": cfg.option_theses.hold_cutoff_calendar_days},
            bars_to_hold, hold_cutoff,
        ),
    ]
    for n in pol_cfg.time_checkpoints:
        if n > thesis.duration_days:
            continue  # 7.9.7: a variant whose checkpoint is past end_date isn't computed
        jobs.append((
            f"time_d{n}", policy_rules.time_checkpoint_rules(stop, n, pol_cfg.time_stop_min_gain_pct),
            {"checkpoint_day": n, "min_gain_pct": pol_cfg.time_stop_min_gain_pct}, bars_to_end, thesis.end_date,
        ))

    for name, rules, params, bars, boundary_date in jobs:
        outcome, exit_time, exit_premium, pending = _policy_outcome(
            entry, target, bars, entry_date, holidays, rules, slippage, boundary_date, contract_expired,
        )
        days_held = pnl = r_multiple = None
        if exit_time is not None and exit_premium is not None:
            days_held = trading_days_between(thesis.created_at.date(), exit_time.date(), holidays)
            pnl = (exit_premium - entry) * thesis.lots * thesis.lot_size - charges
            r_multiple = policy_rules.r_multiple_for(entry, stop, exit_premium)
        repo.upsert_option_thesis_policy_result(
            db, thesis.id, name, policy_params=params, outcome=outcome, exit_time=exit_time,
            exit_premium=exit_premium, days_held=days_held, pnl=pnl, r_multiple=r_multiple,
            pending=pending, scored_at=repo.utcnow(),
        )


def _compute_policies(db: Session, cfg: Config, today: date, holidays: set[date]) -> tuple[int, list[dict]]:
    """Step 4 (7.9.6/7.9.7): recomputed for every scored thesis on every
    run (not gated on end_date like the thesis's own outcome), since
    `hold` can stay pending well past end_date, and a config change to the
    checkpoint list or disaster level should apply to old theses too."""
    computed = 0
    errors: list[dict] = []
    for thesis in repo.list_option_theses_for_policy_compute(db):
        try:
            _compute_policies_for_thesis(db, cfg, thesis, today, holidays)
            computed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"thesis_id": thesis.id, "step": "policies", "message": str(exc)})
    return computed, errors


def run_option_theses_sync(db: Session, cfg: Config) -> dict:
    """Entry point called from theses/sync.py's run_sync() as new steps,
    inside its existing advisory lock -- not a separate locked run."""
    today = date.today()
    now = now_ist()
    holidays = set(cfg.holidays.dates)

    counts = {
        "option_bars_upserted": 0, "option_theses_auto_cancelled": 0, "option_theses_resolved": 0,
        "option_theses_scored": 0, "option_policies_computed": 0,
    }
    errors: list[dict] = []

    counts["option_bars_upserted"], e = _fetch_option_minute_bars(db, cfg, today, now, holidays)
    errors += e
    counts["option_theses_auto_cancelled"], e = _auto_cancel_unlinked_real(db, today)
    errors += e
    counts["option_theses_resolved"], e = _resolve_open_theses(db, cfg, today, now)
    errors += e
    counts["option_theses_scored"], e = _score_closed_theses(db, cfg, today, now, holidays)
    errors += e
    counts["option_policies_computed"], e = _compute_policies(db, cfg, today, holidays)
    errors += e

    return {"counts": counts, "errors": errors}
