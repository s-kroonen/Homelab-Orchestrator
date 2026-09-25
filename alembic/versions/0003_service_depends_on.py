"""add service.depends_on — model the gateway as a scan blocker

Revision ID: 0003_service_depends_on
Revises: 0002_backup_excluded
Create Date: 2026-09-10 00:00:00

The orchestrator often cannot reach guests directly — it goes through a gateway
that also runs the reverse proxy. When that gateway is down, every downstream
probe fails for a network reason. Without modelling the dependency, a single
gateway outage is indistinguishable from every service breaking at once.

Stored as a JSON list of slugs rather than a join table: it is a small, ordered,
YAML-shaped list that is always read whole, and a join table would add a
migration and a query for no gain.

Additive with a server default, so safe on a populated database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_service_depends_on"
down_revision: str | None = "0002_backup_excluded"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.add_column(
            sa.Column(
                "depends_on",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.drop_column("depends_on")
