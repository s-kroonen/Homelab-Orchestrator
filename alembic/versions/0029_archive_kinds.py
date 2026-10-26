"""archive: proxy hosts and backup policies too

Revision ID: 0029_archive_kinds
Revises: 0028_archive
Create Date: 2026-10-26 15:00:00

Additive: existing archive entries are services.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_archive_kinds"
down_revision: str | None = "0028_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("archived_service") as batch:
        batch.add_column(sa.Column("kind", sa.String(), nullable=False, server_default="service"))
        batch.add_column(sa.Column("ref", sa.String(), nullable=False, server_default=""))
    op.create_index("ix_archived_service_kind", "archived_service", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_archived_service_kind", table_name="archived_service")
    with op.batch_alter_table("archived_service") as batch:
        batch.drop_column("ref")
        batch.drop_column("kind")
