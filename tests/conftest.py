"""Pytest fixtures.

We do NOT run migrations in tests — ``SQLModel.metadata.create_all`` is faster
and independent of alembic. Alembic itself is exercised in
``tests/test_db_migrations.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlmodel import Session

from orchestrator import config as config_module
from orchestrator.db import session as session_module


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test a fresh Settings + a fresh on-disk SQLite database.

    ``monkeypatch`` sets env vars BEFORE Settings is instantiated in the test,
    and we reset the singleton cache so each test picks up its own values.
    """
    db_path = tmp_path / "test.db"
    yaml_path = tmp_path / "services.yaml"

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SERVICES_YAML_PATH", str(yaml_path))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("RUN_MIGRATIONS_ON_START", "false")
    monkeypatch.setenv("LOG_FORMAT", "console")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("SESSION_SECRET", "test-only")
    # Don't inherit any developer's stray adapter config.
    for key in ("POWER_ADAPTER", "PROXMOX_ADAPTER", "PBS_ADAPTER"):
        os.environ.pop(key, None)

    config_module.reset_settings_cache()
    session_module.reset_engine_cache()

    yield

    session_module.reset_engine_cache()
    config_module.reset_settings_cache()


@pytest.fixture
def session() -> Iterator[Session]:
    """A DB session backed by a schema built via ``create_all`` (no alembic)."""
    session_module.create_all_for_tests()
    with Session(session_module.get_engine()) as s:
        yield s
