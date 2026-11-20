"""POST/GET /option-theses and friends. Per DESIGN.md 7.9.3/7.9.4/7.9.6/
7.9.8/7.9.10. Also owns GET /sync/latest and GET /sectors, both generic
but small enough not to warrant their own module.

Theses on the option contract's own premium (entry/target/stop). "Run
check" itself is the existing POST /api/v1/checks -- creation here takes
an already-run check_id, mirroring how section 7.8's order ticket takes a
check_id rather than re-deriving a provider itself."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from optionguard.analysis.metrics import _pick_iv_price
from optionguard.api.errors import ApiError
from optionguard.api.schemas import (
    OptionThesisCancelIn, OptionThesisExitIn, OptionThesisIn, OptionThesisLinkTradeIn, OptionThesisNotesIn,
    OptionThesisPreviewIn,
)
from optionguard.api.serializers import option_thesis_policy_result_to_dict, option_thesis_score_to_dict, option_thesis_to_dict
from optionguard.config import get_config
from optionguard.db import repo
from optionguard.db.session import get_db
from optionguard.market.base import NotAvailable
from optionguard.market.factory import (
    apply_kite_session, get_kite_client, get_kite_provider, get_mock_kite_provider, has_valid_kite_session,
    is_kite_mock,
)
from optionguard.market.kite import KiteProvider
from optionguard.market.manual import ManualInput, ManualProvider
from optionguard.market.symbols import CandidateParseError, parse_candidate
from optionguard.option_theses import derived as derived_mod
from optionguard.option_theses import scorecard as scorecard_mod
from optionguard.option_theses.resolve import find_bar_at, is_late_manual_exit, validate_manual_exit_price
from optionguard.orders.gtt import round_to_tick
from optionguard.pricing.calendar import IST, is_market_hours, trading_day_after, trading_day_on_or_before, trading_days_between, trading_days_to_expiry, year_fraction
from optionguard.pricing.iv import implied_vol
from optionguard.rules import guardrails
from optionguard.sync import SyncAlreadyRunningError, run_sync
from optionguard.web.csrf import verify_csrf

router = APIRouter(prefix="/api/v1", tags=["option-theses"])
ui_router = APIRouter(prefix="/ui", tags=["option-theses-ui"], dependencies=[Depends(verify_csrf)])

CHECK_STALE_AFTER = timedelta(minutes=30)


def _resolve_provider(cfg, body, db: Session, parsed):
    """Mirrors api/theses.py's _resolve_provider."""
    if body.source == "manual":
        if body.manual is None:
            raise ApiError("MANUAL_DATA_REQUIRED", "source='manual' requires a 'manual' payload.", status_code=422)
        provider = ManualProvider(
            ManualInput(
                symbol=parsed.symbol, expiry=body.manual.expiry, strike=parsed.strike, opt_type=parsed.opt_type,
                spot=body.manual.spot, premium=body.manual.premium, lot_size=body.manual.lot_size,
                bid=body.manual.bid, ask=body.manual.ask,
            )
        )
        return provider, body.manual.expiry

    apply_kite_session(db)
    if not has_valid_kite_session(db):
        raise ApiError(
            "KITE_LOGIN_REQUIRED", "No valid Kite session -- log in first.",
            status_code=409, login_url=get_kite_client().login_url(),
        )
    provider = get_kite_provider()
    try:
        expiry = provider.resolve_expiry(parsed.symbol, parsed.month)
    except NotAvailable as exc:
        raise ApiError("NO_LISTED_EXPIRY", str(exc)) from exc
    return provider, expiry


def _provider_for_data_source(data_source: str):
    return get_mock_kite_provider() if data_source == "mock" else get_kite_provider()


def _resolve_sector(cfg, symbol: str, requested: str | None) -> tuple[str, str]:
    sector = requested or cfg.symbol_sectors.get(symbol)
    if not sector:
        raise ApiError(
            "SECTOR_REQUIRED", f"No default sector configured for {symbol}; specify one. Valid: {sorted(cfg.sectors)}",
            status_code=422,
        )
    if sector not in cfg.sectors:
        raise ApiError("UNKNOWN_SECTOR", f"Unknown sector {sector!r}. Valid: {sorted(cfg.sectors)}", status_code=422)
    return sector, cfg.sectors[sector]


def _implied_vol_for_check(cfg, check, today: date, holidays: set[date]) -> float | None:
    if check.iv is not None:
        return float(check.iv)
    days_to_expiry = trading_days_to_expiry(today, check.expiry, holidays)
    T_expiry = year_fraction(max(days_to_expiry, 0) or 1, cfg.pricing.trading_days_per_year)
    price = float(check.ask) if check.ask is not None else float(check.premium)
    return implied_vol(price, float(check.spot), float(check.strike), T_expiry, cfg.pricing.risk_free_rate, check.opt_type)


@router.post("/option-theses/preview")
def post_option_thesis_preview(body: OptionThesisPreviewIn, db: Session = Depends(get_db)):
    """7.9.4: live derived numbers for the new-thesis form, without saving."""
    cfg = get_config()
    today = date.today()
    holidays = set(cfg.holidays.dates)

    try:
        parsed = parse_candidate(body.contract)
    except CandidateParseError as exc:
        raise ApiError("PARSE_ERROR", str(exc), status_code=422) from exc

    if not (cfg.option_theses.min_duration_days <= body.duration_days <= cfg.option_theses.max_duration_days):
        raise ApiError(
            "DURATION_OUT_OF_RANGE",
            f"duration_days must be between {cfg.option_theses.min_duration_days} and {cfg.option_theses.max_duration_days}.",
            status_code=422,
        )

    provider, expiry = _resolve_provider(cfg, body, db, parsed)

    end_date = trading_day_after(today, body.duration_days, holidays)
    hold_cutoff = trading_day_on_or_before(expiry - timedelta(days=cfg.option_theses.hold_cutoff_calendar_days), holidays)
    if end_date > hold_cutoff:
        raise ApiError(
            "DURATION_TOO_LONG",
            f"end_date ({end_date}) must be on or before the hold cutoff date ({hold_cutoff}).", status_code=422,
        )

    spot = provider.spot(parsed.symbol)
    quote = provider.option_quote(parsed.symbol, expiry, parsed.strike, parsed.opt_type)
    entry_premium = quote.ask if quote.ask is not None else quote.last_price

    if not (body.target_premium > entry_premium):
        raise ApiError("INVALID_TARGET", "target_premium must be greater than entry_premium.", status_code=422)
    if not (0 < body.stop_premium < entry_premium):
        raise ApiError("INVALID_STOP", "stop_premium must be greater than 0 and less than entry_premium.", status_code=422)

    days_to_expiry = trading_days_to_expiry(today, expiry, holidays)
    T_expiry = year_fraction(max(days_to_expiry, 0) or 1, cfg.pricing.trading_days_per_year)
    price_for_iv, _iv_from_ltp = _pick_iv_price(quote)
    r = cfg.pricing.risk_free_rate
    iv = implied_vol(price_for_iv, spot, parsed.strike, T_expiry, r, parsed.opt_type)
    if iv is None:
        raise ApiError("NO_IV", "Could not imply volatility for this contract from the live quote.", status_code=409)

    derived = derived_mod.compute_derived(
        entry=entry_premium, target=body.target_premium, stop=body.stop_premium, lot_size=quote.lot_size,
        lots=body.lots, max_loss_per_trade=cfg.defaults.max_loss_per_trade, spot=spot, strike=parsed.strike,
        opt_type=parsed.opt_type, r=r, iv=iv, duration_days=body.duration_days, time_to_expiry_entry=T_expiry,
        mc_paths=cfg.option_theses.mc_paths, mc_steps_per_day=cfg.option_theses.mc_steps_per_day,
        mc_seed=cfg.option_theses.mc_seed, trading_days_per_year=cfg.pricing.trading_days_per_year,
    )
    disaster_risk_per_lot = derived_mod.disaster_risk_per_lot(
        entry_premium, cfg.option_theses.policies.disaster_stop_pct, quote.lot_size,
    )
    return {
        "symbol": parsed.symbol, "expiry": expiry.isoformat(), "strike": parsed.strike, "opt_type": parsed.opt_type,
        "spot": spot, "entry_premium": entry_premium, "iv": iv, "lot_size": quote.lot_size,
        "disaster_risk_per_lot": disaster_risk_per_lot, **derived,
    }


@router.post("/option-theses")
def post_option_thesis(body: OptionThesisIn, db: Session = Depends(get_db)):
    cfg = get_config()
    today = date.today()
    holidays = set(cfg.holidays.dates)
    settings = repo.ensure_default_settings(db, cfg)

    check = repo.get_check(db, body.check_id)
    if check is None:
        raise ApiError("NOT_FOUND", f"check {body.check_id} not found", status_code=404)
    if repo.utcnow() - check.created_at > CHECK_STALE_AFTER:
        raise ApiError("CHECK_STALE", "This check is more than 30 minutes old -- run it again.", status_code=409)

    existing = repo.get_open_option_thesis_for_contract(db, check.symbol, check.expiry, float(check.strike), check.opt_type)
    if existing is not None:
        raise ApiError(
            "OPEN_THESIS_EXISTS", f"An open option thesis already exists for this contract (id={existing.id}).",
            status_code=409,
        )

    if check.verdict == "red" and not (body.override_reason and len(body.override_reason) >= 15):
        raise ApiError(
            "OVERRIDE_REQUIRED", "override_reason (>=15 characters) is required for a red-verdict contract.",
            status_code=422,
        )

    if not (cfg.option_theses.min_duration_days <= body.duration_days <= cfg.option_theses.max_duration_days):
        raise ApiError(
            "DURATION_OUT_OF_RANGE",
            f"duration_days must be between {cfg.option_theses.min_duration_days} and {cfg.option_theses.max_duration_days}.",
            status_code=422,
        )

    entry_from_ltp = check.ask is None
    entry_premium = float(check.ask) if check.ask is not None else float(check.premium)
    target_premium = round_to_tick(body.target_premium)
    stop_premium = round_to_tick(body.stop_premium)

    if not (target_premium > entry_premium):
        raise ApiError("INVALID_TARGET", "target_premium must be greater than entry_premium.", status_code=422)
    if not (0 < stop_premium < entry_premium):
        raise ApiError("INVALID_STOP", "stop_premium must be greater than 0 and less than entry_premium.", status_code=422)
    if body.mode == "real":
        guardrails.guard_stop_no_looser_than_settings(stop_premium, entry_premium, settings)

    end_date = trading_day_after(today, body.duration_days, holidays)
    hold_cutoff = trading_day_on_or_before(check.expiry - timedelta(days=cfg.option_theses.hold_cutoff_calendar_days), holidays)
    if end_date > hold_cutoff:
        raise ApiError(
            "DURATION_TOO_LONG",
            f"end_date ({end_date}) must be on or before the hold cutoff date ({hold_cutoff}).", status_code=422,
        )

    sector, sector_index = _resolve_sector(cfg, check.symbol, body.sector)

    loss_vs_cap = derived_mod.loss_vs_cap(entry_premium, stop_premium, check.lot_size, body.lots, cfg.defaults.max_loss_per_trade)
    if body.mode == "real" and loss_vs_cap > 1.0:
        raise ApiError(
            "LOSS_EXCEEDS_CAP", f"Risk for {body.lots} lots exceeds max_loss_per_trade (loss_vs_cap={loss_vs_cap:.2f}).",
            status_code=422,
        )
    warnings: list[str] = []
    if body.lots > check.max_lots:
        warnings.append(f"{body.lots} lots exceeds this check's max_lots ({check.max_lots}).")
    if body.mode == "paper" and loss_vs_cap > 1.0:
        warnings.append(f"Risk for {body.lots} lots exceeds max_loss_per_trade (loss_vs_cap={loss_vs_cap:.2f}).")

    iv = _implied_vol_for_check(cfg, check, today, holidays)
    days_to_expiry = trading_days_to_expiry(today, check.expiry, holidays)
    T_expiry = year_fraction(max(days_to_expiry, 0) or 1, cfg.pricing.trading_days_per_year)
    reward_risk = derived_mod.reward_risk(entry_premium, target_premium, stop_premium)

    spot_for_target = spot_for_stop = None
    mkt_prob_target_first = mkt_prob_stop_first = mkt_prob_time_exit = None
    if iv is not None:
        time_to_expiry_mid = T_expiry - (body.duration_days / 2) / cfg.pricing.trading_days_per_year
        spot_for_target = derived_mod.spot_for_premium(target_premium, float(check.strike), time_to_expiry_mid, cfg.pricing.risk_free_rate, iv, check.opt_type)
        spot_for_stop = derived_mod.spot_for_premium(stop_premium, float(check.strike), time_to_expiry_mid, cfg.pricing.risk_free_rate, iv, check.opt_type)
        odds = derived_mod.market_implied_odds(
            spot=float(check.spot), strike=float(check.strike), opt_type=check.opt_type, r=cfg.pricing.risk_free_rate,
            iv=iv, time_to_expiry_entry=T_expiry, duration_days=body.duration_days, target_premium=target_premium,
            stop_premium=stop_premium, mc_paths=cfg.option_theses.mc_paths, mc_steps_per_day=cfg.option_theses.mc_steps_per_day,
            mc_seed=cfg.option_theses.mc_seed, trading_days_per_year=cfg.pricing.trading_days_per_year,
        )
        mkt_prob_target_first = odds["mkt_prob_target_first"]
        mkt_prob_stop_first = odds["mkt_prob_stop_first"]
        mkt_prob_time_exit = odds["mkt_prob_time_exit"]

    provider = None
    if check.data_source in ("kite", "mock"):
        provider = _provider_for_data_source(check.data_source)
    opt_instrument_token = None
    if provider is not None and isinstance(provider, KiteProvider):
        try:
            opt_instrument_token = provider.option_instrument_token(check.symbol, check.expiry, float(check.strike), check.opt_type)
        except NotAvailable:
            pass
    sector_index_entry = market_index_entry = None
    if provider is not None:
        try:
            sector_index_entry = provider.spot(sector_index)
        except NotAvailable:
            pass
        try:
            market_index_entry = provider.spot(cfg.option_theses.market_benchmark)
        except NotAvailable:
            pass

    entry_flags = []
    if entry_from_ltp:
        entry_flags.append("entry_from_ltp")
    if not is_market_hours(datetime.now(timezone.utc)):
        entry_flags.append("entry_offhours")

    override_id = None
    if check.verdict == "red":
        override = repo.add_override(db, check_id=check.id, trade_id=None, rule="RED_VERDICT_THESIS", reason=body.override_reason)
        override_id = override.id

    check_snapshot = {
        "verdict": check.verdict, "flags": check.flags, "spot": float(check.spot),
        "bid": float(check.bid) if check.bid is not None else None, "ask": float(check.ask) if check.ask is not None else None,
        "iv": iv, "delta": float(check.delta) if check.delta is not None else None,
        "theta_per_day_lot": float(check.theta_per_day_lot) if check.theta_per_day_lot is not None else None,
        "prob_profit": float(check.prob_profit) if check.prob_profit is not None else None,
        "spread_pct": float(check.spread_pct) if check.spread_pct is not None else None, "lot_size": check.lot_size,
    }

    thesis = repo.create_option_thesis(
        db, symbol=check.symbol, expiry=check.expiry, strike=float(check.strike), opt_type=check.opt_type,
        opt_instrument_token=opt_instrument_token, mode=body.mode, data_source=check.data_source, sector=sector,
        sector_index=sector_index, why=body.why, check_id=check.id, verdict=check.verdict, check_snapshot=check_snapshot,
        override_id=override_id, lot_size=check.lot_size, lots=body.lots, entry_premium=entry_premium,
        target_premium=target_premium, stop_premium=stop_premium, duration_days=body.duration_days, end_date=end_date,
        reward_risk=reward_risk, spot_for_target=spot_for_target, spot_for_stop=spot_for_stop,
        mkt_prob_target_first=mkt_prob_target_first, mkt_prob_stop_first=mkt_prob_stop_first,
        mkt_prob_time_exit=mkt_prob_time_exit, spot_entry=float(check.spot), sector_index_entry=sector_index_entry,
        market_index_entry=market_index_entry, entry_flags=entry_flags, notes=body.notes,
    )

    out = option_thesis_to_dict(thesis)
    out["warnings"] = warnings
    if body.mode == "real":
        out["next"] = f"/ui/order?check_id={check.id}&thesis_id={thesis.id}"
    return out


@router.get("/option-theses")
def get_option_theses(
    status: str | None = None, mode: str | None = None, sector: str | None = None, symbol: str | None = None,
    verdict: str | None = None, from_date: date | None = None, to_date: date | None = None,
    db: Session = Depends(get_db),
):
    rows = repo.list_option_theses(db, status=status, mode=mode, sector=sector, symbol=symbol, verdict=verdict, date_from=from_date, date_to=to_date)
    return [option_thesis_to_dict(r) for r in rows]


@router.get("/option-theses/scorecard")
def get_option_theses_scorecard(
    from_date: date | None = None, to_date: date | None = None, mode: str = "all", group_by: str = "none",
    db: Session = Depends(get_db),
):
    """7.9.8. Must be declared before /option-theses/{thesis_id} -- FastAPI
    matches routes in declaration order."""
    if group_by not in scorecard_mod.GROUP_BY_OPTIONS:
        raise ApiError("BAD_GROUP_BY", f"group_by must be one of {sorted(scorecard_mod.GROUP_BY_OPTIONS)}", status_code=422)
    if mode not in ("paper", "real", "all"):
        raise ApiError("BAD_MODE", "mode must be one of paper, real, all", status_code=422)

    cfg = get_config()
    pairs = repo.list_scored_option_theses_with_scores(db, mode=mode, date_from=from_date, date_to=to_date)
    n_cancelled_total = repo.count_cancelled_option_theses(db, mode=mode, date_from=from_date, date_to=to_date)

    policy_results_by_thesis: dict[int, dict] = {}
    for r in repo.list_option_thesis_policy_results_for_theses(db, [t.id for t, _ in pairs]):
        policy_results_by_thesis.setdefault(r.thesis_id, {})[r.policy] = r

    groups: dict[str, list[tuple]] = {}
    for thesis, score in pairs:
        groups.setdefault(scorecard_mod.group_key(thesis, group_by), []).append((thesis, score))

    out = {}
    for key, group_pairs in sorted(groups.items()):
        stats = scorecard_mod.group_stats(group_pairs, n_cancelled=n_cancelled_total if group_by == "none" else 0)
        policy_stats = scorecard_mod.policy_block(group_pairs, policy_results_by_thesis)
        out[key] = {
            **stats,
            "policies": policy_stats,
            "independence": scorecard_mod.independence_note(group_pairs),
            "findings": scorecard_mod.findings(group_pairs, stats, policy_stats, cfg.option_theses.findings_min_n),
        }
    if not out:
        out["all" if group_by == "none" else "_summary"] = {
            "n": 0, "n_cancelled": n_cancelled_total,
            "findings": [f"Not enough scored theses yet (n=0; need {cfg.option_theses.findings_min_n})."],
        }
    return out


@router.get("/option-theses/{thesis_id}")
def get_option_thesis(thesis_id: int, db: Session = Depends(get_db)):
    row = repo.get_option_thesis(db, thesis_id)
    if row is None:
        raise ApiError("NOT_FOUND", f"option thesis {thesis_id} not found", status_code=404)
    out = option_thesis_to_dict(row)
    score = repo.get_option_thesis_score(db, thesis_id)
    out["score"] = option_thesis_score_to_dict(score) if score else None
    out["policies"] = [option_thesis_policy_result_to_dict(r) for r in repo.list_option_thesis_policy_results(db, thesis_id)]

    out["interim"] = None
    if row.status == "open":
        cfg = get_config()
        holidays = set(cfg.holidays.dates)
        interim = {"days_left": trading_days_between(date.today(), row.end_date, holidays), "live_premium": None}
        if row.data_source in ("kite", "mock"):
            try:
                provider = _provider_for_data_source(row.data_source)
                quote = provider.option_quote(row.symbol, row.expiry, float(row.strike), row.opt_type)
                interim["live_premium"] = quote.ask if quote.ask is not None else quote.last_price
            except NotAvailable:
                pass
        out["interim"] = interim
    return out


@router.patch("/option-theses/{thesis_id}/notes")
def patch_option_thesis_notes(thesis_id: int, body: OptionThesisNotesIn, db: Session = Depends(get_db)):
    if repo.get_option_thesis(db, thesis_id) is None:
        raise ApiError("NOT_FOUND", f"option thesis {thesis_id} not found", status_code=404)
    row = repo.update_option_thesis(db, thesis_id, notes=body.notes)
    return option_thesis_to_dict(row)


@router.post("/option-theses/resolve")
def post_option_theses_resolve(db: Session = Depends(get_db)):
    """7.9.6's Resolve & score button. This runs the same sync pipeline as
    the Kite-login/CLI sync (optionguard.sync.run_sync, which already
    includes the option theses steps) -- there's only one sync, not two."""
    apply_kite_session(db)
    if not (is_kite_mock() or has_valid_kite_session(db)):
        raise ApiError(
            "KITE_LOGIN_REQUIRED", "No valid Kite session -- log in first.",
            status_code=409, login_url=get_kite_client().login_url(),
        )
    try:
        return run_sync(trigger="button")
    except SyncAlreadyRunningError as exc:
        raise ApiError("SYNC_RUNNING", str(exc), status_code=409) from exc


@router.get("/sync/latest")
def get_sync_latest(db: Session = Depends(get_db)):
    row = repo.get_latest_sync_run(db)
    if row is None:
        return None
    return {
        "id": row.id, "started_at": row.started_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "trigger": row.trigger, "status": row.status, "counts": row.counts, "errors": row.errors,
    }


@router.get("/sectors")
def get_sectors():
    """Sectors available for an option thesis's `sector` field (7.9.3)."""
    cfg = get_config()
    return {"sectors": cfg.sectors}


def _require_option_thesis(db: Session, thesis_id: int):
    row = repo.get_option_thesis(db, thesis_id)
    if row is None:
        raise ApiError("NOT_FOUND", f"option thesis {thesis_id} not found", status_code=404)
    return row


@ui_router.post("/option-theses/{thesis_id}/exit")
def post_option_thesis_exit(thesis_id: int, body: OptionThesisExitIn, db: Session = Depends(get_db)):
    """7.9.5's manual early exit (paper only)."""
    cfg = get_config()
    row = _require_option_thesis(db, thesis_id)
    if row.mode != "paper" or row.status != "open":
        raise ApiError("NOT_EXITABLE", "Only an open paper thesis can be manually exited.", status_code=409)

    exit_time = datetime.fromisoformat(body.exit_time)
    if exit_time.tzinfo is None:
        exit_time = exit_time.replace(tzinfo=IST)

    bars = repo.list_option_minute_bars(db, row.opt_instrument_token, from_ts=exit_time, to_ts=exit_time)
    from optionguard.option_theses.resolve import Bar

    bar_rows = [Bar(ts=b.ts, o=float(b.o), h=float(b.h), l=float(b.l), c=float(b.c)) for b in bars if b.o is not None]
    bar = find_bar_at(bar_rows, exit_time)
    if bar is None:
        raise ApiError("NO_BAR_AT_TIME", "No bar found at that exit time -- sync bars first.", status_code=422)
    if not validate_manual_exit_price(body.price, bar):
        raise ApiError(
            "PRICE_OUT_OF_RANGE", f"{body.price} is outside this bar's range ({bar.l}-{bar.h}).", status_code=422,
        )

    now = repo.utcnow()
    reason_text = f"{body.reason}: {body.text}" if body.text else body.reason
    updated = repo.update_option_thesis(
        db, thesis_id, outcome="manual", exit_time=exit_time, exit_premium=body.price,
        manual_exit_reason=reason_text, manual_exit_entered_at=now, status="closed",
    )
    out = option_thesis_to_dict(updated)
    out["late_entry"] = is_late_manual_exit(exit_time, now, cfg.option_theses.late_exit_flag_days)
    return out


@ui_router.post("/option-theses/{thesis_id}/cancel")
def post_option_thesis_cancel(thesis_id: int, body: OptionThesisCancelIn, db: Session = Depends(get_db)):
    row = _require_option_thesis(db, thesis_id)
    if row.status != "open":
        raise ApiError("NOT_OPEN", f"option thesis {thesis_id} is not open (status={row.status}).", status_code=409)
    updated = repo.update_option_thesis(db, thesis_id, status="cancelled", cancelled_at=repo.utcnow(), cancel_reason=body.reason)
    return option_thesis_to_dict(updated)


@ui_router.post("/option-theses/{thesis_id}/link-trade")
def post_option_thesis_link_trade(thesis_id: int, body: OptionThesisLinkTradeIn, db: Session = Depends(get_db)):
    """7.9.5: linking a real thesis to a trade recorded with "Record a
    trade placed elsewhere" -- only until orders are enabled (static IP)."""
    row = _require_option_thesis(db, thesis_id)
    if row.mode != "real" or row.status != "open":
        raise ApiError("NOT_LINKABLE", "Only an open, real option thesis can be linked to a trade.", status_code=409)
    if row.trade_id is not None:
        raise ApiError("THESIS_ALREADY_LINKED", f"Thesis {thesis_id} is already linked to trade {row.trade_id}.", status_code=409)

    trade = repo.get_trade(db, body.trade_id)
    if trade is None:
        raise ApiError("NOT_FOUND", f"trade {body.trade_id} not found", status_code=404)
    check = repo.get_check(db, trade.check_id)
    if (
        check.symbol != row.symbol or check.expiry != row.expiry
        or float(check.strike) != float(row.strike) or check.opt_type != row.opt_type
    ):
        raise ApiError("THESIS_CONTRACT_MISMATCH", "The trade's contract doesn't match this thesis's contract.", status_code=422)
    if trade.entry_time.date() != row.created_at.date():
        raise ApiError("NOT_SAME_DAY", "The trade must have been entered on the thesis's own entry day.", status_code=422)

    updated = repo.update_option_thesis(db, thesis_id, trade_id=trade.id, entry_premium=float(trade.entry_premium))
    return option_thesis_to_dict(updated)
