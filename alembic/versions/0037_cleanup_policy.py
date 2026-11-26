"""cleanup policies: prune + garbage collection of a PBS datastore (OR-96)

Revision ID: 0037_cleanup_policy
Revises: 0036_guest_facts
Create Date: 2026-11-23 14:00:00

* ``cleanup_policy`` - what to prune and when to garbage-collect a datastore, and what to
  wake for it. See docs/cleanup.md.
* ``backup_policy.cleanup_policy`` / ``cleanup_when`` - the cleanup a backup policy runs in
  the same wake, before or after its backups. Enum stored by member NAME (see 0018).

Additive: no backup policy runs a cleanup until one is set.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_cleanup_policy"
down_revision: str | None = "0036_guest_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cleanup_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("datastore", sa.String(), nullable=False, server_default=""),
        sa.Column("schedule_cron", sa.String(), nullable=False, server_default=""),
        sa.Column("prune", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("prune_unowned_keep_last", sa.Integer(), nullable=True),
        sa.Column("gc_mode", sa.String(), nullable=False, server_default="INTERVAL"),
        sa.Column("gc_interval_h", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("gc_above_pct", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("max_wait_h", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("requires", sa.JSON(), nullable=True),
        sa.Column("wake_mode", sa.String(), nullable=False, server_default="FOLLOW"),
        sa.Column("restore_power_state", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_cleanup_policy_name", "cleanup_policy", ["name"], unique=True)
    with op.batch_alter_table("backup_policy") as batch:
        batch.add_column(
            sa.Column("cleanup_policy", sa.String(), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("cleanup_when", sa.String(), nullable=False, server_default="BEFORE")
        )


def downgrade() -> None:
    with op.batch_alter_table("backup_policy") as batch:
        batch.drop_column("cleanup_when")
        batch.drop_column("cleanup_policy")
    op.drop_index("ix_cleanup_policy_name", table_name="cleanup_policy")
    op.drop_table("cleanup_policy")
