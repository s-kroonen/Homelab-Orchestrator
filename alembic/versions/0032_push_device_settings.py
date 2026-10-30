"""push_subscription: a device's own settings (alerts on or paused, the least severe it gets)

Revision ID: 0032_push_device_settings
Revises: 0031_probe_backup_waits
Create Date: 2026-10-30 14:00:00

Additive: every existing device stays on and keeps getting everything the rules send to push.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_push_device_settings"
down_revision: str | None = "0031_probe_backup_waits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("push_subscription") as batch:
        batch.add_column(
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.add_column(
            sa.Column("min_severity", sa.String(), nullable=False, server_default="info")
        )


def downgrade() -> None:
    with op.batch_alter_table("push_subscription") as batch:
        batch.drop_column("min_severity")
        batch.drop_column("enabled")
