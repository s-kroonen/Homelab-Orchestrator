"""service.wake_trigger - call (any request wakes it) or button (a press does)

Revision ID: 0017_service_wake_trigger
Revises: 0016_probe_enabled
Create Date: 2026-10-22 12:00:00

Additive with a server default of ``call``, which is exactly how the
maintenance page behaved before, so nothing changes until an operator picks
``button`` for a service. See docs/proxy_hosts.md.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_service_wake_trigger"
down_revision: str | None = "0016_probe_enabled"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.add_column(
            sa.Column("wake_trigger", sa.String(), nullable=False, server_default="call"),
        )


def downgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.drop_column("wake_trigger")
