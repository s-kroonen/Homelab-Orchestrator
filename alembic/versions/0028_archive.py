"""archive: soft-deleted services

Revision ID: 0028_archive
Revises: 0027_dumps
Create Date: 2026-10-26 12:00:00

Additive: a new table, nothing existing changes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_archive"
down_revision: str | None = "0027_dumps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "archived_service",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False, server_default=""),
        sa.Column("parent_slug", sa.String(), nullable=True),
        sa.Column("sub_slugs", sa.JSON(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_by", sa.String(), nullable=False, server_default=""),
        sa.Column("purge_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_archived_service_slug", "archived_service", ["slug"])
    op.create_index("ix_archived_service_deleted_at", "archived_service", ["deleted_at"])
    op.create_index("ix_archived_service_purge_at", "archived_service", ["purge_at"])


def downgrade() -> None:
    op.drop_table("archived_service")
