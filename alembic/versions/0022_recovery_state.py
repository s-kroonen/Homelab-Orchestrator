"""recovery_state - where each service's automatic recovery stands

Revision ID: 0022_recovery_state
Revises: 0021_notifications
Create Date: 2026-10-23 14:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_recovery_state"
down_revision: str | None = "0021_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recovery_state",
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("service.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("failing_scans", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_started_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_action", sa.String(), nullable=False, server_default=""),
        sa.Column("last_result", sa.String(), nullable=False, server_default=""),
        sa.Column("exhausted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("recovery_state")
