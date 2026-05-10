"""Database connection + SQLAlchemy 2.0 session management.

Postgres-only. We use SQLAlchemy Core for queries (not the ORM) because the
schema is small and explicit SQL stays close to what the Apps Script version
did with sheet rows. Alembic handles migrations.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

log = logging.getLogger(__name__)


def _make_engine() -> Engine:
    """Create the SQLAlchemy engine. Render's DATABASE_URL is `postgresql://...`,
    which SQLAlchemy now wants as `postgresql+psycopg://...` for psycopg3."""
    url = settings.database_url
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        echo=settings.app_env == "development" and settings.log_level == "DEBUG",
    )


engine: Engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(Engine, "connect")
def _set_pg_application_name(dbapi_connection, connection_record):  # noqa: D401
    """Tag connections so they show up identifiably in `pg_stat_activity`."""
    try:
        with dbapi_connection.cursor() as cur:
            cur.execute("SET application_name = 'auto-screener'")
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def db_session() -> Iterator[Session]:
    """Context-managed SQLAlchemy session. Commits on success, rolls back on error."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
