"""Selects the Kite client (mock or real) once per process, so the rest of
the app only ever depends on KiteProvider / MarketProvider."""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from optionguard.config import get_config
from optionguard.market.kite import KiteProvider
from optionguard.market.mock_kite_client import MockKiteClient

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def is_kite_mock() -> bool:
    return not get_config().secrets.kite_api_key


@lru_cache
def get_kite_client() -> Any:
    if is_kite_mock():
        return MockKiteClient(api_key="mock")
    from kiteconnect import KiteConnect

    return KiteConnect(api_key=get_config().secrets.kite_api_key)


def has_valid_kite_session(db: Session) -> bool:
    if is_kite_mock():
        return True
    from optionguard.db import repo

    db_session = repo.get_kite_session(db)
    return bool(db_session and db_session.access_token and db_session.valid_for == date.today())


def apply_kite_session(db: Session) -> None:
    """Real mode only: push today's stored access_token onto the client so
    quote()/instruments() calls authenticate. No-op for the mock client,
    which never needs a session. Call this before using get_kite_provider()
    in any request path that might hit real Kite."""
    if is_kite_mock():
        return
    from optionguard.db import repo

    db_session = repo.get_kite_session(db)
    if db_session and db_session.access_token and db_session.valid_for == date.today():
        get_kite_client().set_access_token(db_session.access_token)


def get_kite_provider() -> KiteProvider:
    return KiteProvider(get_kite_client())


_mock_provider: KiteProvider | None = None


def get_mock_kite_provider() -> KiteProvider:
    """Always mock-backed, regardless of the current KITE_API_KEY state.

    Checks/trades recorded while in mock mode are tagged data_source='mock'
    (see api/checks.py). Re-fetching live-ish data for one later -- e.g. on
    the Positions screen, or in `backfill-settle` -- must keep using mock
    data even after real credentials are added; otherwise a historically
    mock-tagged candidate would get looked up against the real market,
    which may not even have a matching contract."""
    global _mock_provider
    if _mock_provider is None:
        _mock_provider = KiteProvider(MockKiteClient(api_key="mock"))
    return _mock_provider


def effective_data_source(requested_source: str) -> str:
    """What to actually store/use for a "kite" request: "mock" if no real
    credentials are configured, "kite" otherwise. "manual" passes through
    unchanged."""
    if requested_source == "kite" and is_kite_mock():
        return "mock"
    return requested_source
