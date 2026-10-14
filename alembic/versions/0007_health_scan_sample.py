"""add health_scan_sample — periodic health-scan history

Revision ID: 0007_health_scan_sample
Revises: 0006_wake_on_hit
Create Date: 2026-10-14 00:00:00

``HealthEngine.scan`` was, until now, only ever called from three places —
the CLI's manual ``scan`` command, ``WakePipeline``, and ``BackupPipeline``'s
gate check — none of them on any schedule, and each starting its OWN fresh
dependency cache. ``ServiceState`` (one row per service, holding only the
LATEST verdict) therefore only updated as a side effect of something
unrelated, and two services sharing a dependency (e.g. a gateway) could each
re-probe it live at a different real moment and persist mutually
inconsistent verdicts for the SAME dependency.

``orchestrator.scheduler.health_scanner.HealthScanner`` fixes this by running
one scan cycle on its own schedule, sharing ONE dependency cache across every
top-level service scanned that cycle — a shared dependency is probed exactly
once per cycle, and every dependent sees the same verdict for it.

``health_scan_sample`` is the history that decision needs to be visible on
the dashboard: a compact trend ("was this healthy an hour ago?"), not a
forensic record — the full per-probe detail for the latest scan already
lives on ``service_state.last_probe_results``, and a specific probe can be
re-run on demand (the ``probe-test`` tool). Deliberately short-retention and
narrow-columned so this does not become a second audit log; see
``settings.health_history_retention_days`` and
``HealthScanner._prune``. CASCADE on delete, same reasoning as
``traffic_sample``: a removed service's health history is not worth keeping
around as an orphan.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_health_scan_sample"
down_revision: str | None = "0006_wake_on_hit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "health_scan_sample",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("service.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sampled_at", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("reason", sa.String(), nullable=False, server_default=""),
    )
    op.create_index("ix_health_scan_sample_service_id", "health_scan_sample", ["service_id"])
    op.create_index("ix_health_scan_sample_sampled_at", "health_scan_sample", ["sampled_at"])


def downgrade() -> None:
    op.drop_index("ix_health_scan_sample_sampled_at", table_name="health_scan_sample")
    op.drop_index("ix_health_scan_sample_service_id", table_name="health_scan_sample")
    op.drop_table("health_scan_sample")
