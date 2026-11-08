"""admin.session_version

Revision ID: 0034_admin_session_version
Revises: 0033_detection_fields
Create Date: 2026-11-08 22:00:00

Additive: every admin starts at 0, which is what a session signed in before this carries.
Raising it ends every session that admin has (disabling them, OR-56; "sign out everywhere"
later, OR-7).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_admin_session_version"
down_revision: str | None = "0033_detection_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("admin") as batch:
        batch.add_column(
            sa.Column("session_version", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("admin") as batch:
        batch.drop_column("session_version")
