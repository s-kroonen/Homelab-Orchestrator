"""add service.backup_excluded — the circular-backup guard

Revision ID: 0002_backup_excluded
Revises: 0001_baseline
Create Date: 2026-09-09 00:00:00

Adds an explicit, non-overridable "never back this up" flag, distinct from
``enabled``:

* ``enabled=False``        -> do not manage the service at all
* ``backup_excluded=True`` -> manage it (wake, health, dashboard) but never
                              back it up, not even on a manual trigger

The motivating case is a VM that hosts the storage PBS itself lives on: dumping
it to PBS writes the backup into the thing being backed up.

Additive and nullable-free (server defaults supplied), so the upgrade is safe on
a populated database and the downgrade loses only these two columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_backup_excluded"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table so SQLite (which cannot ALTER ADD COLUMN with all
    # constraint forms) is handled via table rebuild where needed.
    with op.batch_alter_table("service") as batch:
        batch.add_column(
            sa.Column(
                "backup_excluded",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column(
                "backup_excluded_reason",
                sa.String(),
                nullable=False,
                server_default="",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.drop_column("backup_excluded_reason")
        batch.drop_column("backup_excluded")
