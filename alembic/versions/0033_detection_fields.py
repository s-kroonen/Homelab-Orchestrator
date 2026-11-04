"""probe.needs_internet, notification_rule.first_check

Revision ID: 0033_detection_fields
Revises: 0032_push_device_settings
Create Date: 2026-11-03 10:00:00

Additive: no probe needs the internet and no rule sends on the first check until told to.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_detection_fields"
down_revision: str | None = "0032_push_device_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("probe") as batch:
        batch.add_column(
            sa.Column("needs_internet", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    with op.batch_alter_table("notification_rule") as batch:
        batch.add_column(
            sa.Column("first_check", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("notification_rule") as batch:
        batch.drop_column("first_check")
    with op.batch_alter_table("probe") as batch:
        batch.drop_column("needs_internet")
