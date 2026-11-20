"""The shared sync job -- the concurrency lock and the login-triggers-sync
wiring. Per DESIGN.md 7.8.6/7.9.6 (this module used to also cover section
7.7's now-removed thesis-journal steps)."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from optionguard.api import kite_auth
from optionguard.market import factory
from optionguard import sync as sync_mod
from optionguard.sync import SyncAlreadyRunningError, run_sync


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setattr(factory, "is_kite_mock", lambda: True)
    factory.get_kite_client.cache_clear()
    return factory.get_kite_provider()


def test_concurrent_sync_returns_409(db, mock_mode):
    from optionguard.db.session import get_engine

    engine = get_engine()
    lock_conn = engine.connect()
    try:
        acquired = lock_conn.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": sync_mod.SYNC_ADVISORY_LOCK_KEY}
        ).scalar()
        assert acquired is True
        with pytest.raises(SyncAlreadyRunningError):
            run_sync(trigger="cli")
    finally:
        lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": sync_mod.SYNC_ADVISORY_LOCK_KEY})
        lock_conn.close()

    # Lock released -- a normal sync now goes through.
    result = run_sync(trigger="cli")
    assert result["status"] in ("ok", "partial")


def test_login_callback_triggers_exactly_one_background_sync(client, mock_mode, monkeypatch):
    calls = []
    monkeypatch.setattr(kite_auth, "run_sync", lambda trigger: calls.append(trigger))

    resp = client.get("/kite/callback", params={"request_token": "whatever"})
    assert resp.status_code == 200
    assert calls == ["login"]
