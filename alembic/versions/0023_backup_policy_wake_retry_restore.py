"""backup policy: wake_mode, max_attempts, restore_power_state

Revision ID: 0023_backup_policy_opts
Revises: 0022_recovery_state
Create Date: 2026-10-24 09:00:00

* ``wake_mode`` - whether a backup honours the wake-on-hit switches of what it wakes
  (FOLLOW), ignores them (ALWAYS) or wakes nothing (NEVER). Enum column, stored by
  member NAME, so the default is upper case (see 0018).
* ``max_attempts`` - tries before a blocked / not-healthy run gives up (default 3).
* ``restore_power_state`` - stop what the backup started and power its node back off.

Additive; every existing policy keeps working (and starts returning nodes to the
state they were in, which is what it always claimed to).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_backup_policy_opts"
down_revision: str | None = "0022_recovery_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("backup_policy") as batch:
        batch.add_column(
            sa.Column("wake_mode", sa.String(), nullable=False, server_default="FOLLOW")
        )
        batch.add_column(
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3")
        )
        batch.add_column(
            sa.Column(
                "restore_power_state", sa.Boolean(), nullable=False, server_default=sa.true()
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("backup_policy") as batch:
        batch.drop_column("restore_power_state")
        batch.drop_column("max_attempts")
        batch.drop_column("wake_mode")
