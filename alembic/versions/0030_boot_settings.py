"""boot settings: guests' start at boot / order, nodes' start-on-boot delay

Revision ID: 0030_boot_settings
Revises: 0029_archive_kinds
Create Date: 2026-10-28 12:00:00

Additive: a new table and two nullable columns, nothing existing changes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_boot_settings"
down_revision: str | None = "0029_archive_kinds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "guest_boot_setting",
        sa.Column("node", sa.String(), primary_key=True),
        sa.Column("vmid", sa.Integer(), primary_key=True),
        sa.Column("onboot", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("order", sa.Integer(), nullable=True),
        sa.Column("up_s", sa.Integer(), nullable=True),
        sa.Column("down_s", sa.Integer(), nullable=True),
        sa.Column("read_at", sa.DateTime(), nullable=True),
    )
    with op.batch_alter_table("node_state") as batch:
        batch.add_column(sa.Column("onboot_delay_s", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("boot_read_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("node_state") as batch:
        batch.drop_column("boot_read_at")
        batch.drop_column("onboot_delay_s")
    op.drop_table("guest_boot_setting")
