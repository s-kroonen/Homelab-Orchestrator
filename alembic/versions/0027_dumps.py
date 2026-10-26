"""dumps: service.dump and the dump_record table

Revision ID: 0027_dumps
Revises: 0026_mcp
Create Date: 2026-10-25 18:00:00

Additive: every existing service starts with no dump ({}).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_dumps"
down_revision: str | None = "0026_mcp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.add_column(sa.Column("dump", sa.JSON(), nullable=False, server_default="{}"))
    op.create_table(
        "dump_record",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "service_id", sa.Integer(), sa.ForeignKey("service.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("service_slug", sa.String(), nullable=False),
        sa.Column(
            "pipeline_run_id",
            sa.Integer(),
            sa.ForeignKey("pipeline_run.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(), nullable=False, server_default=""),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("protected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_dump_record_service_slug", "dump_record", ["service_slug"])
    op.create_index("ix_dump_record_protected", "dump_record", ["protected"])
    op.create_index("ix_dump_record_created_at", "dump_record", ["created_at"])


def downgrade() -> None:
    op.drop_table("dump_record")
    with op.batch_alter_table("service") as batch:
        batch.drop_column("dump")
