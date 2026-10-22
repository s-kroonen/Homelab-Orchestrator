"""service tree: parent_id, controller, recovery, backup_gate

Revision ID: 0020_service_tree
Revises: 0019_node_address
Create Date: 2026-10-23 10:00:00

A sub-service is a service with a parent (a container on Portainer, an add-on on
Home Assistant). Additive; every existing service becomes a root with no
controller, no recovery and backup_gate IGNORE.

NOTE: ``backup_gate`` is an Enum column, which SQLModel stores by member NAME
(``IGNORE``), not value - the default must be upper case (see 0018).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_service_tree"
down_revision: str | None = "0019_node_address"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.add_column(sa.Column("parent_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("controller", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("recovery", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(
            sa.Column("backup_gate", sa.String(), nullable=False, server_default="IGNORE")
        )
        batch.create_foreign_key(
            "fk_service_parent_id", "service", ["parent_id"], ["id"], ondelete="CASCADE"
        )


def downgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.drop_constraint("fk_service_parent_id", type_="foreignkey")
        batch.drop_column("backup_gate")
        batch.drop_column("recovery")
        batch.drop_column("controller")
        batch.drop_column("parent_id")
