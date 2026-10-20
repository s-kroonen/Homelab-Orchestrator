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


def test_0013_adds_proxy_host_enabled_defaulting_true_and_drops_it_on_downgrade() -> None:
    import sqlalchemy as sa

    cfg = _cfg()
    command.upgrade(cfg, "0012_proxy_host_route_options")
    engine = sa.create_engine(get_settings().database_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO proxy_host (hostname, upstream, router_provider, extra, "
                "wake_on_hit, entry_points, cert_resolver, route_options) "
                "VALUES ('a.example.com', '', 'TRAEFIK', '{}', 0, '[]', '', '{}')"
            )
        )
    command.upgrade(cfg, "0013_proxy_host_enabled")
    with engine.connect() as conn:
        cols = {c["name"] for c in sa.inspect(conn).get_columns("proxy_host")}
        assert "enabled" in cols
        # An existing host stays switched on.
        assert conn.execute(sa.text("SELECT enabled FROM proxy_host")).scalar_one() in (1, True)
    command.downgrade(cfg, "0012_proxy_host_route_options")
    with engine.connect() as conn:
        cols = {c["name"] for c in sa.inspect(conn).get_columns("proxy_host")}
        assert "enabled" not in cols
    engine.dispose()
