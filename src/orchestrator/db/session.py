"""Database engine + session factory.

SQLite is the target for now. WAL mode + foreign-key enforcement + a longer
busy timeout are set for every connection so a hot dashboard read never
collides with a scheduled write.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from orchestrator.config import Settings, get_settings

_engine: Engine | None = None


def _configure_sqlite(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    """Per-connection PRAGMAs applied to every SQLite handle we hand out."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def build_engine(settings: Settings | None = None) -> Engine:
    """Construct and cache the SQLAlchemy engine."""
    global _engine
    if _engine is not None:
        return _engine

    settings = settings or get_settings()

    connect_args: dict[str, object] = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    _engine = create_engine(
        settings.database_url,
        echo=False,
        connect_args=connect_args,
    )

    if settings.database_url.startswith("sqlite"):
        event.listen(_engine, "connect", _configure_sqlite)

    return _engine


def get_engine() -> Engine:
    if _engine is None:
        return build_engine()
    return _engine


def reset_engine_cache() -> None:
    """Test helper — dispose and forget the engine."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all_for_tests() -> None:
    """Bypass Alembic — used only from tests to spin up an empty schema."""
    # Import for side effects so SQLModel.metadata is populated.
    from orchestrator.db import models  # noqa: F401

    SQLModel.metadata.create_all(get_engine())
