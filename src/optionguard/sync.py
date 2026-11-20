"""The sync that replaces a cron job: reconcile order/GTT state (7.8.6),
settle expired checks (the original backfill-settle behaviour), and run the
option-theses pipeline (7.9.6). Self-contained -- manages its own DB
connection and session rather than accepting one from the caller, because
the advisory lock must be acquired and released on the *same* physical
connection, which a request-scoped SQLAlchemy Session doesn't guarantee
across commits (repo.* functions commit per call)."""
from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from optionguard.config import get_config
from optionguard.db import repo
from optionguard.db.session import get_engine, get_sessionmaker
from optionguard.market.base import MarketProvider
from optionguard.market.factory import get_kite_provider, get_mock_kite_provider
from optionguard.option_theses.sync import run_option_theses_sync
from optionguard.orders import service as orders_service
from optionguard.pricing.bs import intrinsic_value

SYNC_ADVISORY_LOCK_KEY = 772201  # arbitrary, stable


class SyncAlreadyRunningError(Exception):
    pass


def _provider_for(data_source: str) -> MarketProvider:
    return get_mock_kite_provider() if data_source == "mock" else get_kite_provider()


def _settle_checks(db: Session, today: date) -> tuple[int, list[dict]]:
    """The backfill-settle logic for `checks` -- `optionguard backfill-settle`
    runs only this step, as an alias."""
    pending = repo.checks_pending_settlement(db, today)
    settled = 0
    errors: list[dict] = []
    for check in pending:
        if check.data_source not in ("kite", "mock"):
            continue
        try:
            provider = _provider_for(check.data_source)
            bars = provider.history(check.symbol, days=400)
            match = next((b for b in bars if b.day == check.expiry), None)
            if match is None:
                continue
            check.expiry_settle = intrinsic_value(match.close, float(check.strike), check.opt_type)
            check.settled_at = repo.utcnow()
            db.commit()
            settled += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            errors.append({"check_id": check.id, "step": "settle", "message": str(exc)})
    return settled, errors


def run_sync(trigger: str) -> dict:
    """Returns the finished sync_runs row as a dict. Raises
    SyncAlreadyRunningError if another sync holds the advisory lock."""
    engine = get_engine()
    lock_conn = engine.connect()
    try:
        acquired = lock_conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": SYNC_ADVISORY_LOCK_KEY}).scalar()
        if not acquired:
            raise SyncAlreadyRunningError("a sync is already running")

        db = get_sessionmaker()()
        try:
            cfg = get_config()
            today = date.today()
            run = repo.create_sync_run(db, trigger)
            counts = {
                "checks_settled": 0, "exits_recorded": 0, "unprotected": 0, "intents_resumed": 0,
                "option_bars_upserted": 0, "option_theses_auto_cancelled": 0, "option_theses_resolved": 0,
                "option_theses_scored": 0, "option_policies_computed": 0,
            }
            all_errors: list[dict] = []
            try:
                # Step 0 (section 7.8.6): reconcile GTT/order state before
                # anything else -- a triggered stop that filled needs to
                # close the trade before option theses scoring looks at it.
                reconcile_result = orders_service.reconcile(db, cfg, today=today)
                counts["exits_recorded"] = reconcile_result["exits_recorded"]
                counts["unprotected"] = reconcile_result["unprotected"]
                counts["intents_resumed"] = reconcile_result["intents_resumed"]
                all_errors += reconcile_result["errors"]

                counts["checks_settled"], settle_errors = _settle_checks(db, today)
                all_errors += settle_errors

                # Step 2 (section 7.9.6): option theses -- bars, resolve, score, policies.
                opt_result = run_option_theses_sync(db, cfg)
                counts.update(opt_result["counts"])
                all_errors += opt_result["errors"]

                status = "ok" if not all_errors else "partial"
            except Exception as exc:  # noqa: BLE001 -- a bug here still must not leave the run stuck "running"
                all_errors.append({"step": "sync", "message": str(exc)})
                status = "failed"
            finished = repo.finish_sync_run(db, run.id, status=status, counts=counts, errors=all_errors)
            return {
                "id": finished.id, "started_at": finished.started_at.isoformat(),
                "finished_at": finished.finished_at.isoformat() if finished.finished_at else None,
                "trigger": finished.trigger, "status": finished.status,
                "counts": finished.counts, "errors": finished.errors,
            }
        finally:
            db.close()
    finally:
        try:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": SYNC_ADVISORY_LOCK_KEY})
        finally:
            lock_conn.close()
