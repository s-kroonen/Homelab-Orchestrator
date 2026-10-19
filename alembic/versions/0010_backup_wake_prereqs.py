"""add backup wake prerequisites — policy.requires / wake_before_backup,
service.backup_requires

Revision ID: 0010_backup_wake_prereqs
Revises: 0009_traffic_rollup
Create Date: 2026-10-19 00:00:00

A backup used to check that PBS was reachable and then dump; nothing woke
anything, so on a low-power homelab with burst nodes the run simply failed
when a node was off. Two additions:

* ``backup_policy.requires`` / ``service.backup_requires`` — JSON lists of
  service slugs that must be up (woken, then held) before the backup starts.
  The canonical case is the storage VM hosting the NFS the PBS datastore
  lives on. Distinct from ``service.depends_on`` (health-gate dependency).
* ``backup_policy.wake_before_backup`` — wake the services being backed up
  before their backup. Defaults TRUE, so existing policies pick the behaviour
  up on upgrade; with no prerequisites and an already-up node it is a no-op.

Additive with server defaults, so safe on a populated database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_backup_wake_prereqs"
down_revision: str | None = "0009_traffic_rollup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("backup_policy") as batch:
        batch.add_column(
            sa.Column("requires", sa.JSON(), nullable=False, server_default="[]"),
        )
        batch.add_column(
            sa.Column(
                "wake_before_backup", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
        )
    with op.batch_alter_table("service") as batch:
        batch.add_column(
            sa.Column("backup_requires", sa.JSON(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.drop_column("backup_requires")
    with op.batch_alter_table("backup_policy") as batch:
        batch.drop_column("wake_before_backup")
        batch.drop_column("requires")
