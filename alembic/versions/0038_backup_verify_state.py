"""a verify still running in PBS is followed, not failed (OR-98)

Revision ID: 0038_backup_verify_state
Revises: 0037_cleanup_policy
Create Date: 2026-11-26 10:30:00

* ``backup_record.verify_upid`` - PBS's task id of a verify that outlasted its deadline.
* ``backup_record.verify_state`` - ``ok`` / ``failed`` / ``running`` (PBS still verifying,
  followed by the queue) / ``unknown`` (it stopped being followed); empty for records from
  before this, where ``verified`` alone tells.

Additive.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_backup_verify_state"
down_revision: str | None = "0037_cleanup_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("backup_record") as batch:
        batch.add_column(sa.Column("verify_upid", sa.String(), nullable=True))
        batch.add_column(sa.Column("verify_state", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("backup_record") as batch:
        batch.drop_column("verify_state")
        batch.drop_column("verify_upid")
