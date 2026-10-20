"""proxy_host.enabled

Revision ID: 0013_proxy_host_enabled
Revises: 0012_proxy_host_route_options
Create Date: 2026-10-19 20:00:00

A proxy host can now be switched off without deleting it. For a live-managed
host the route is parked on Traefik Manager (its ``POST /api/routes/<id>/toggle``
moves the router into its own ``disabled_routes`` store), so Traefik stops
serving it while every setting is kept for Enable.

Additive with a server default of true, so safe on a populated database:
every existing host stays enabled.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_proxy_host_enabled"
down_revision: str | None = "0012_proxy_host_route_options"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("proxy_host") as batch:
        batch.add_column(
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        )


def downgrade() -> None:
    with op.batch_alter_table("proxy_host") as batch:
        batch.drop_column("enabled")
