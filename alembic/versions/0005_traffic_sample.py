"""add traffic_sample — per-router Traefik traffic snapshots

Revision ID: 0005_traffic_sample
Revises: 0004_service_group
Create Date: 2026-10-14 00:00:00

Traefik exposes request counters at ``/metrics`` (Prometheus text format,
``--metrics.prometheus=true``), but it keeps no history of its own — a
counter resets to 0 whenever Traefik restarts, and a single snapshot only
ever says "total requests since the process last started." To show any
traffic trend at all (the per-service/per-host dashboard, the overall
requests/min figure) this app has to poll periodically and store the raw
counter value itself.

``traffic_sample`` holds one row per (proxy_host, poll) with the RAW
cumulative counter — never a precomputed delta — so a reader can divide
``(b.requests_total - a.requests_total)`` by the elapsed time between any
two rows to get a rate over whatever window it wants; that is robust to a
missed poll (a longer window, not corrupt data) and needs no separate
"first ever sample" special case.

Only rows that could be attributed to an existing ``proxy_host`` are ever
written (see ``orchestrator.scheduler.traffic_collector``), so
``proxy_host_id`` is NOT NULL, unlike ``proxy_host.service_id`` which is
nullable. CASCADE on delete: a removed proxy host's traffic history is not
worth keeping around as an orphan.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_traffic_sample"
down_revision: str | None = "0004_service_group"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "traffic_sample",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "proxy_host_id",
            sa.Integer(),
            sa.ForeignKey("proxy_host.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sampled_at", sa.DateTime(), nullable=False),
        sa.Column("requests_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_sum_s", sa.Float(), nullable=False, server_default="0"),
        sa.Column("duration_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("by_code", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_traffic_sample_proxy_host_id", "traffic_sample", ["proxy_host_id"])
    op.create_index("ix_traffic_sample_sampled_at", "traffic_sample", ["sampled_at"])


def downgrade() -> None:
    op.drop_index("ix_traffic_sample_sampled_at", table_name="traffic_sample")
    op.drop_index("ix_traffic_sample_proxy_host_id", table_name="traffic_sample")
    op.drop_table("traffic_sample")
