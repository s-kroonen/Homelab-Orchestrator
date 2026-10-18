"""add traffic_rollup — per-minute access-log counters per proxy host

Revision ID: 0009_traffic_rollup
Revises: 0008_geo_traffic_sample
Create Date: 2026-10-18 00:00:00

``/traffic`` used to read ``traffic_sample`` (Traefik's Prometheus counters,
fetched straight from ``TRAEFIK_API_URL``). Operators who run Traefik
Manager as their source of truth often leave that URL empty, which left the
page blank. The access log — already tailed through Traefik Manager for the
GeoIP view — carries everything ``/traffic`` needs, so the access-log
collector now rolls it up into one row per ``(proxy_host, UTC minute)``
here. ``duration_ms_sum``/``duration_ms_count``/``duration_ms_max`` keep
latency re-aggregatable into coarser buckets. CASCADE on delete, same as
``traffic_sample``. Pruned after ``TRAFFIC_ROLLUP_RETENTION_DAYS``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_traffic_rollup"
down_revision: str | None = "0008_geo_traffic_sample"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "traffic_rollup",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "proxy_host_id",
            sa.Integer(),
            sa.ForeignKey("proxy_host.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bucket_start", sa.DateTime(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("s2xx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("s3xx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("s4xx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("s5xx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms_sum", sa.Float(), nullable=False, server_default="0"),
        sa.Column("duration_ms_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms_max", sa.Float(), nullable=True),
        sa.UniqueConstraint("proxy_host_id", "bucket_start", name="uq_traffic_rollup_host_bucket"),
    )
    op.create_index("ix_traffic_rollup_bucket_start", "traffic_rollup", ["bucket_start"])


def downgrade() -> None:
    op.drop_index("ix_traffic_rollup_bucket_start", table_name="traffic_rollup")
    op.drop_table("traffic_rollup")
