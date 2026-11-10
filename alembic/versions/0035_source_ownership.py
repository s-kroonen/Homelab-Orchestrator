"""node.source_id, proxy_host.source_id

Revision ID: 0035_source_ownership
Revises: 0034_admin_session_version
Create Date: 2026-11-09 10:00:00

Which source (connection id) a node or proxy host belongs to (OR-77). Blank until the
orchestrator claims it for the default source of its kind at start (connectors/ownership.py).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_source_ownership"
down_revision: str | None = "0034_admin_session_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("node", "proxy_host"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("source_id", sa.String(), nullable=False, server_default=""))
            batch.create_index(f"ix_{table}_source_id", ["source_id"])


def downgrade() -> None:
    for table in ("proxy_host", "node"):
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{table}_source_id")
            batch.drop_column("source_id")
