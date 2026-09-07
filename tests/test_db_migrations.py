"""Alembic upgrade + downgrade must both succeed (reversibility contract)."""

from __future__ import annotations

from alembic import command
from alembic.config import Config

from orchestrator.config import get_settings


def _cfg() -> Config:
    settings = get_settings()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


def test_upgrade_head_then_downgrade_base_is_clean() -> None:
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    # A second upgrade proves nothing was left behind by downgrade.
    command.upgrade(cfg, "head")
