"""`optionguard serve`, `optionguard backfill-settle`. Per DESIGN.md section 4."""
from __future__ import annotations

import argparse
import os
from datetime import date, datetime, timezone

from optionguard.config import get_config


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    cfg = get_config()
    # Binding 127.0.0.1 is the right default for a bare `uv run optionguard serve` on
    # your own machine (DESIGN.md: never exposed beyond 127.0.0.1). Inside a container
    # that same bind would make the app unreachable from outside the container entirely
    # -- Docker's port publishing connects to the container's own interface, not its
    # loopback. The Dockerfile sets OPTIONGUARD_HOST=0.0.0.0 and docker-compose.yml
    # publishes the port as "127.0.0.1:8787:8787" on the host side, so the
    # 127.0.0.1-only guarantee still holds from outside the container either way.
    host = os.environ.get("OPTIONGUARD_HOST", "127.0.0.1")
    uvicorn.run("optionguard.app:create_app", factory=True, host=host, port=cfg.server.port, reload=args.reload)


def cmd_backfill_settle(args: argparse.Namespace) -> None:
    """For checks with expiry < today and no expiry_settle, compute intrinsic
    value from the underlying's close on expiry day. Kite/mock-sourced checks
    only -- manual mode has no history to backfill from. A check tagged
    "mock" is settled against mock history, never real history, even if
    real credentials exist now -- its contract may not even be real."""
    from optionguard.db import repo
    from optionguard.db.session import get_sessionmaker
    from optionguard.market.base import NotAvailable
    from optionguard.market.factory import apply_kite_session, get_kite_provider, get_mock_kite_provider
    from optionguard.pricing.bs import intrinsic_value

    db = get_sessionmaker()()
    try:
        today = date.today()
        pending = repo.checks_pending_settlement(db, today)
        if not pending:
            print("Nothing to settle.")
            return

        apply_kite_session(db)
        kite_provider = get_kite_provider()
        mock_provider = get_mock_kite_provider()
        settled = 0
        for check in pending:
            if check.data_source not in ("kite", "mock"):
                continue
            provider = mock_provider if check.data_source == "mock" else kite_provider
            try:
                bars = provider.history(check.symbol, days=400)
            except NotAvailable:
                continue
            match = next((b for b in bars if b.day == check.expiry), None)
            if match is None:
                continue
            check.expiry_settle = intrinsic_value(match.close, float(check.strike), check.opt_type)
            check.settled_at = datetime.now(timezone.utc)
            settled += 1
        db.commit()
        print(f"Settled {settled} of {len(pending)} pending checks.")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="optionguard")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="Run the core service on 127.0.0.1.")
    serve_p.add_argument("--reload", action="store_true")
    serve_p.set_defaults(func=cmd_serve)

    backfill_p = sub.add_parser("backfill-settle", help="Backfill expiry_settle for past checks.")
    backfill_p.set_defaults(func=cmd_backfill_settle)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
