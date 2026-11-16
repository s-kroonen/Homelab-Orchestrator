"""guest_facts: the latest reading of each guest from inside it

Revision ID: 0036_guest_facts
Revises: 0035_source_ownership
Create Date: 2026-11-16 10:00:00

One row per guest (source, key): what its guest agent last reported - addresses, sign-ins,
disks, freeze state, which agent commands are on. Runtime only (guests/store.py).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_guest_facts"
down_revision: str | None = "0035_source_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "guest_facts",
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("guest_key", sa.String(), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("source_id", "guest_key"),
    )


def downgrade() -> None:
    op.drop_table("guest_facts")
