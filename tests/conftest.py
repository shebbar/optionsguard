"""Tests run against a real Postgres database -- a sibling "<db>_test" of
whatever DATABASE_URL already points at (derived, never hardcoded, since
this file is committed to a public repo). The test database is created
once if missing and its tables are truncated between tests."""
from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import pytest
import sqlalchemy as sa


def _base_db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    from optionguard.config import Secrets

    return Secrets().database_url


def _as_test_url(url: str) -> str:
    parts = urlsplit(url)
    db_name = parts.path.lstrip("/") or "optionguard"
    test_path = f"/{db_name}_test"
    return urlunsplit((parts.scheme, parts.netloc, test_path, parts.query, parts.fragment))


_BASE_URL = _base_db_url()
_TEST_URL = _as_test_url(_BASE_URL)
os.environ["DATABASE_URL"] = _TEST_URL


def _ensure_test_database() -> None:
    import psycopg

    base_parts = urlsplit(_BASE_URL)
    test_db_name = urlsplit(_TEST_URL).path.lstrip("/")
    admin_dsn = urlunsplit((base_parts.scheme.replace("+psycopg", ""), base_parts.netloc, "/postgres", "", ""))
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (test_db_name,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{test_db_name}"')


@pytest.fixture(scope="session", autouse=True)
def _db_setup():
    _ensure_test_database()
    from optionguard.db.models import Base
    from optionguard.db.session import get_engine

    engine = get_engine()
    # Drop and recreate every session -- this DB is throwaway, and create_all
    # alone won't pick up constraint changes on tables that already exist
    # from an earlier test run against a stale schema. metadata.drop_all()
    # itself can get stuck the same way: it diffs against *current* model
    # metadata, so a use_alter FK that didn't exist in an older test-DB
    # schema makes it try to drop a constraint that was never created.
    # Resetting the whole schema sidesteps that instead of chasing it.
    with engine.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


@pytest.fixture
def db():
    from optionguard.db.models import Base
    from optionguard.db.session import get_engine, get_sessionmaker

    engine = get_engine()
    session = get_sessionmaker()()
    yield session
    session.close()
    with engine.begin() as conn:
        # TRUNCATE ... CASCADE handles FK ordering across all tables in one statement.
        names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(sa.text(f"TRUNCATE TABLE {names} CASCADE"))


@pytest.fixture(autouse=True)
def _reset_kite_client_cache():
    """get_kite_client() is @lru_cache'd with no args, so whichever client
    (mock or real) a test first triggers would otherwise leak into every
    later test regardless of monkeypatched is_kite_mock() -- clear it both
    sides of each test."""
    from optionguard.market import factory

    factory.get_kite_client.cache_clear()
    factory._mock_provider = None
    yield
    factory.get_kite_client.cache_clear()
    factory._mock_provider = None


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from optionguard.app import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def mock_mode(monkeypatch):
    """Forces mock-backed Kite behaviour regardless of real credentials in
    the ambient .env, and returns the always-mock KiteProvider -- shared by
    the section 7.7 and 7.8 test modules that each used to define this
    locally."""
    from optionguard.market import factory

    monkeypatch.setattr(factory, "is_kite_mock", lambda: True)
    factory.get_kite_client.cache_clear()
    return factory.get_mock_kite_provider()


@pytest.fixture
def orders_enabled():
    """[orders].enabled is a mutable attribute on the lru_cache'd Config
    singleton -- flip it on for a test, then always flip it back off so it
    can't leak into a later test."""
    from optionguard.config import get_config

    cfg = get_config()
    cfg.orders.enabled = True
    yield cfg
    cfg.orders.enabled = False
