"""push_subscription: devices that turned on Web Push notifications

Revision ID: 0024_push_subscription
Revises: 0023_backup_policy_opts
Create Date: 2026-10-24 14:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_push_subscription"
down_revision: str | None = "0023_backup_policy_opts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscription",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("p256dh", sa.String(), nullable=False),
        sa.Column("auth", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False, server_default=""),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("user_agent", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_ok_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=False, server_default=""),
    )
    op.create_index("ix_push_subscription_endpoint", "push_subscription", ["endpoint"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_push_subscription_endpoint", table_name="push_subscription")
    op.drop_table("push_subscription")
