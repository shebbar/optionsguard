"""`optionguard serve`, `optionguard sync`, `optionguard backfill-settle`.
Per DESIGN.md section 4, 7.8.6 and 7.9.6."""
from __future__ import annotations

import argparse
import os


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from optionguard.config import get_config

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


def cmd_sync(args: argparse.Namespace) -> None:
    """Sections 7.8.6 and 7.9.6: the full sync (reconcile orders/GTTs,
    settle checks, option theses bars/resolve/score/policies), trigger="cli"."""
    from optionguard.sync import SyncAlreadyRunningError, run_sync

    try:
        result = run_sync(trigger="cli")
    except SyncAlreadyRunningError:
        print("A sync is already running.")
        return
    print(f"Sync {result['status']}: {result['counts']}")
    if result["errors"]:
        print(f"{len(result['errors'])} error(s):")
        for err in result["errors"]:
            print(f"  {err}")


def cmd_backfill_settle(args: argparse.Namespace) -> None:
    """Alias that runs only the settle step of `sync` -- kept for anyone
    used to running this on its own, e.g. from an existing cron entry."""
    from datetime import date

    from optionguard.db.session import get_sessionmaker
    from optionguard.sync import _settle_checks

    db = get_sessionmaker()()
    try:
        settled, errors = _settle_checks(db, date.today())
        print(f"Settled {settled} check(s).")
        for err in errors:
            print(f"  {err}")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="optionguard")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="Run the core service on 127.0.0.1.")
    serve_p.add_argument("--reload", action="store_true")
    serve_p.set_defaults(func=cmd_serve)

    sync_p = sub.add_parser("sync", help="Run the full sync: reconcile orders/GTTs, settle checks, option theses.")
    sync_p.set_defaults(func=cmd_sync)

    backfill_p = sub.add_parser("backfill-settle", help="Alias for just the settle step of `sync`.")
    backfill_p.set_defaults(func=cmd_backfill_settle)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
