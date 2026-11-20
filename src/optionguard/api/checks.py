"""POST /checks, GET /checks/{id}. Per DESIGN.md section 11."""
from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from optionguard.analysis import alternatives as alt_mod
from optionguard.analysis import explain
from optionguard.analysis import oi as oi_mod
from optionguard.analysis import scenarios as scenarios_mod
from optionguard.analysis import sizing
from optionguard.analysis import verdict as verdict_mod
from optionguard.analysis.metrics import CandidateRequest, compute_metrics
from optionguard.api.errors import ApiError
from optionguard.api.schemas import ChecksRequest
from optionguard.api.serializers import check_to_dict
from optionguard.config import get_config
from optionguard.db import repo
from optionguard.db.session import get_db
from optionguard.market.base import NotAvailable
from optionguard.market.factory import (
    apply_kite_session, effective_data_source, get_kite_client, get_kite_provider, has_valid_kite_session,
)
from optionguard.market.manual import ManualInput, ManualProvider
from optionguard.market.symbols import CandidateParseError, parse_candidate, parse_thesis
from optionguard.util import to_jsonable

router = APIRouter(prefix="/api/v1", tags=["checks"])


@router.post("/checks")
def post_checks(body: ChecksRequest, db: Session = Depends(get_db)):
    cfg = get_config()
    settings = repo.ensure_default_settings(db, cfg)
    today = date.today()
    batch_id = str(uuid.uuid4())

    shared_provider = None
    source_tag = effective_data_source(body.source)  # "mock" instead of "kite" if no real credentials
    if body.source == "kite":
        apply_kite_session(db)
        if not has_valid_kite_session(db):
            raise ApiError(
                "KITE_LOGIN_REQUIRED", "No valid Kite session -- log in first.",
                status_code=409, login_url=get_kite_client().login_url(),
            )
        shared_provider = get_kite_provider()

    results = []
    for cand in body.candidates:
        try:
            parsed = parse_candidate(cand.text)
        except CandidateParseError as exc:
            raise ApiError("PARSE_ERROR", str(exc)) from exc

        thesis_target, thesis_date = parsed.thesis_target, parsed.thesis_date
        if cand.thesis:
            thesis_target, thesis_date = parse_thesis(cand.thesis)

        if body.source == "manual":
            if cand.manual is None:
                raise ApiError(
                    "MANUAL_DATA_REQUIRED", f"{cand.text}: manual source requires a 'manual' payload."
                )
            provider = ManualProvider(
                ManualInput(
                    symbol=parsed.symbol, expiry=cand.manual.expiry, strike=parsed.strike,
                    opt_type=parsed.opt_type, spot=cand.manual.spot, premium=cand.manual.premium,
                    lot_size=cand.manual.lot_size, bid=cand.manual.bid, ask=cand.manual.ask,
                    hv20=cand.manual.hv20, volume=cand.manual.volume, oi=cand.manual.oi,
                )
            )
            expiry = cand.manual.expiry
        else:
            provider = shared_provider
            try:
                expiry = provider.resolve_expiry(parsed.symbol, parsed.month)
            except NotAvailable as exc:
                raise ApiError("NO_LISTED_EXPIRY", str(exc)) from exc

        events = [e for e in repo.events_before(db, parsed.symbol, expiry) if e.event_date >= today]
        dividends = [
            (e.event_date, float(e.dividend)) for e in events if e.event_type == "dividend" and e.dividend is not None
        ]
        earnings_dates = [e.event_date for e in events if e.event_type == "earnings"]
        has_earnings = bool(earnings_dates)

        req = CandidateRequest(
            symbol=parsed.symbol, expiry=expiry, strike=parsed.strike, opt_type=parsed.opt_type,
            data_source=source_tag, thesis_target=thesis_target, thesis_date=thesis_date,
            manual_hv20=cand.manual.hv20 if cand.manual else None,
        )
        m = compute_metrics(
            provider, req, config=cfg, today=today, dividends=dividends, has_earnings_before_expiry=has_earnings
        )

        scenarios = []
        if m.iv is not None:
            scenarios = scenarios_mod.compute_scenarios(
                spot=m.spot, strike=req.strike, opt_type=req.opt_type, r=cfg.pricing.risk_free_rate, sigma=m.iv,
                lot_size=m.lot_size, entry_price=m.ask if m.ask is not None else m.premium, bid=m.bid, ask=m.ask,
                trading_days_to_expiry=m.trading_days_to_expiry, trading_days_per_year=cfg.pricing.trading_days_per_year,
                today=today, earnings_dates=earnings_dates, holidays=set(cfg.holidays.dates),
            )

        lots = sizing.max_lots(
            m.premium, m.lot_size,
            max_loss_per_trade=float(settings.max_loss_per_trade), premium_stop_pct=float(settings.premium_stop_pct),
        )

        oi_context = None
        if body.source == "kite":
            oi_context = oi_mod.compute_oi_context(provider, parsed.symbol, expiry, m.spot)

        flags = verdict_mod.compute_flags(m, req, cfg.verdict, max_lots=lots, oi_context=oi_context)
        verdict_level = verdict_mod.overall_verdict(flags)

        alternatives = alt_mod.compute_alternatives(
            provider, req, config=cfg, today=today,
            max_loss_per_trade=float(settings.max_loss_per_trade), premium_stop_pct=float(settings.premium_stop_pct),
            dividends=dividends, has_earnings_before_expiry=has_earnings,
        )

        facts = explain.build_explanation_facts(m, req, flags, lots)
        flags_json = to_jsonable([f.to_dict() for f in flags])

        row = repo.create_check(
            db,
            batch_id=batch_id, symbol=parsed.symbol, expiry=expiry, strike=parsed.strike, opt_type=parsed.opt_type,
            data_source=source_tag, spot=m.spot, premium=m.premium, bid=m.bid, ask=m.ask, lot_size=m.lot_size,
            iv=m.iv, hv20=m.hv20, hv_percentile=m.hv_percentile, breakeven=m.breakeven, breakeven_pct=m.breakeven_pct,
            expected_move_pct=m.expected_move_pct, prob_profit=m.prob_profit, prob_zero=m.prob_zero,
            prob_thesis=m.prob_thesis, theta_per_day_lot=m.theta_per_day_lot, delta=m.delta,
            daily_move_needed=m.daily_move_needed, stretch_5d_pct=m.stretch_5d_pct,
            stretch_20dma_pct=m.stretch_20dma_pct, spread_pct=m.spread_pct, thesis_target=thesis_target,
            thesis_date=thesis_date, verdict=verdict_level, flags=flags_json, max_lots=lots,
            alternatives=to_jsonable(alternatives), oi_context=to_jsonable(oi_context),
            raw=to_jsonable({
                "candidate_text": cand.text,
                "manual": cand.manual.model_dump(mode="json") if cand.manual else None,
                "metrics": m,
                "scenarios": scenarios,
            }),
        )

        result = check_to_dict(row)
        result["explanation_facts"] = facts
        results.append(result)

    ranking = [
        r["id"]
        for r in sorted(results, key=lambda r: (r["verdict"] != "green", -(r["prob_profit"] or 0)))
    ]

    return {"batch_id": batch_id, "results": results, "ranking": ranking}


@router.get("/checks/{check_id}")
def get_check(check_id: int, db: Session = Depends(get_db)):
    row = repo.get_check(db, check_id)
    if row is None:
        raise ApiError("NOT_FOUND", f"check {check_id} not found", status_code=404)
    return check_to_dict(row)


@router.delete("/checks/{check_id}")
def delete_check(check_id: int, db: Session = Depends(get_db)):
    row = repo.get_check(db, check_id)
    if row is None:
        raise ApiError("NOT_FOUND", f"check {check_id} not found", status_code=404)
    if check_id in repo.set_of_referenced_check_ids(db):
        raise ApiError(
            "CHECK_HAS_REFERENCES",
            f"check {check_id} led to a trade, thesis, order, or override -- it can't be deleted.",
            status_code=409,
        )
    repo.delete_check(db, check_id)
    return {"deleted": check_id}
