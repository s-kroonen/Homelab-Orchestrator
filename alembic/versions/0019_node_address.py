"""node.address - where a node's own Proxmox API answers

Revision ID: 0019_node_address
Revises: 0018_wake_trigger_enum_names
Create Date: 2026-10-22 16:00:00

Lets each node be health-checked directly through its own Proxmox API. Additive
with a blank default; filled from the cluster listing or by hand.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_node_address"
down_revision: str | None = "0018_wake_trigger_enum_names"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("node") as batch:
        batch.add_column(sa.Column("address", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("node") as batch:
        batch.drop_column("address")
