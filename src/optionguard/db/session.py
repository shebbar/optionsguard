from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from optionguard.config import get_config

_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(get_config().secrets.database_url, pool_pre_ping=True)
    return _engine


def get_sessionmaker() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = get_sessionmaker()()
    try:
        yield db
    finally:
        db.close()
