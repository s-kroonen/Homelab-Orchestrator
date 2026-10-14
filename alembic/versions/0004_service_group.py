"""add service.group — purely organisational tag for the dashboard

Revision ID: 0004_service_group
Revises: 0003_service_depends_on
Create Date: 2026-10-14 00:00:00

As the registry grows (a homelab easily reaches a dozen-plus services), the
flat service table and the backup-policy target checklist both get hard to
scan. ``group`` is a free-form string the operator sets per service; the
dashboard uses it to collapse the service table into sections and to offer
"select this whole group" when picking backup-policy targets. It has no
effect on backups, proxy routing or health — purely organisational.

"" (the default) means ungrouped, not a distinct "Ungrouped" group — kept
as a plain empty string rather than nullable so comparisons and the YAML
round-trip don't need to special-case None vs "".

Additive with a server default, so safe on a populated database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_service_group"
down_revision: str | None = "0003_service_depends_on"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.add_column(
            sa.Column(
                "group",
                sa.String(),
                nullable=False,
                server_default="",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.drop_column("group")
