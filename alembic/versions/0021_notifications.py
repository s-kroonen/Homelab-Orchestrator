"""notification rules, the event feed, and per-target state

Revision ID: 0021_notifications
Revises: 0020_service_tree
Create Date: 2026-10-23 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_notifications"
down_revision: str | None = "0020_service_tree"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_rule",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("levels", sa.JSON(), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("min_severity", sa.String(), nullable=False, server_default="info"),
        sa.Column("channels", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_notification_rule_name", "notification_rule", ["name"], unique=True)
    op.create_table(
        "notification_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("level", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False, server_default=""),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False, server_default="info"),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("body", sa.String(), nullable=False, server_default=""),
        sa.Column("cause", sa.String(), nullable=False, server_default=""),
        sa.Column("suppressed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("suppressed_reason", sa.String(), nullable=False, server_default=""),
        sa.Column("rule_ids", sa.JSON(), nullable=False),
        sa.Column("delivered", sa.JSON(), nullable=False),
    )
    op.create_index("ix_notification_event_at", "notification_event", ["at"])
    op.create_index("ix_notification_event_level", "notification_event", ["level"])
    op.create_index("ix_notification_event_target", "notification_event", ["target"])
    op.create_index("ix_notification_event_event", "notification_event", ["event"])
    op.create_table(
        "target_state",
        sa.Column("level", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("announced", sa.String(), nullable=False, server_default=""),
        sa.Column("since", sa.DateTime(), nullable=False),
        sa.Column("pending_state", sa.String(), nullable=False, server_default=""),
        sa.Column("pending_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transitions", sa.JSON(), nullable=False),
        sa.Column("muted_until", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("level", "key"),
    )


def downgrade() -> None:
    op.drop_table("target_state")
    op.drop_table("notification_event")
    op.drop_table("notification_rule")
