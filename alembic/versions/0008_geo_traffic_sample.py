"""add geo_traffic_sample — GeoIP traffic visibility (opt-in)

Revision ID: 0008_geo_traffic_sample
Revises: 0007_health_scan_sample
Create Date: 2026-10-17 00:00:00

Per-request client-IP/country visibility, sourced from Traefik's own access
log (tailed through Traefik Manager's existing ``GET
/api/traefik/logs?lines=N`` — see ``orchestrator.adapters.proxy.base
.ProxyAdapter.tail_access_log``) rather than the aggregate Prometheus
counters ``traffic_sample`` already stores, since per-request client IPs
only ever appear in the access log, never in the counters.

``geo_traffic_sample`` is the storage half of that: one row per
``(proxy_host, client_ip)`` pair actually seen in a poll cycle, resolved to
a country via ``orchestrator.registry.geoip``. Deliberately short-retention
and narrow-columned, same reasoning as ``health_scan_sample`` — a
monitoring trend view, not a forensic per-request log (the raw access-log
lines already live in Traefik's own log file; this does not duplicate
them). See ``settings.geoip_traffic_retention_days`` and
``orchestrator.scheduler.geo_traffic_collector.GeoTrafficCollector._prune``.
CASCADE on delete, same reasoning as ``traffic_sample``/``health_scan_sample``:
history for a removed proxy host is not worth keeping as an orphan.

Entirely opt-in (``GEOIP_ENABLED=false`` by default) — this table exists
whether or not the feature is turned on, same convention as every other
optional integration's tables in this app, but stays empty until it is.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_geo_traffic_sample"
down_revision: str | None = "0007_health_scan_sample"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "geo_traffic_sample",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "proxy_host_id",
            sa.Integer(),
            sa.ForeignKey("proxy_host.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sampled_at", sa.DateTime(), nullable=False),
        sa.Column("client_ip", sa.String(), nullable=False),
        sa.Column("bucket", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("country_code", sa.String(), nullable=False, server_default=""),
        sa.Column("country_name", sa.String(), nullable=False, server_default=""),
        sa.Column("city", sa.String(), nullable=False, server_default=""),
        sa.Column("status_code", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index(
        "ix_geo_traffic_sample_proxy_host_id", "geo_traffic_sample", ["proxy_host_id"]
    )
    op.create_index("ix_geo_traffic_sample_sampled_at", "geo_traffic_sample", ["sampled_at"])
    op.create_index("ix_geo_traffic_sample_client_ip", "geo_traffic_sample", ["client_ip"])


def downgrade() -> None:
    op.drop_index("ix_geo_traffic_sample_client_ip", table_name="geo_traffic_sample")
    op.drop_index("ix_geo_traffic_sample_sampled_at", table_name="geo_traffic_sample")
    op.drop_index("ix_geo_traffic_sample_proxy_host_id", table_name="geo_traffic_sample")
    op.drop_table("geo_traffic_sample")
