"""probe.backup_waits: a root's backup waits for this check on a sub-service

Revision ID: 0031_probe_backup_waits
Revises: 0030_boot_settings
Create Date: 2026-10-29 10:00:00

Additive: one column, false for every existing probe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_probe_backup_waits"
down_revision: str | None = "0030_boot_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("probe") as batch:
        batch.add_column(
            sa.Column("backup_waits", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("probe") as batch:
        batch.drop_column("backup_waits")
