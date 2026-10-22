"""service.wake_trigger - call (any request wakes it) or button (a press does)

Revision ID: 0017_service_wake_trigger
Revises: 0016_probe_enabled
Create Date: 2026-10-22 12:00:00

Additive with a server default of ``CALL``, which is exactly how the
maintenance page behaved before, so nothing changes until an operator picks
``button`` for a service. See docs/proxy_hosts.md.

NOTE: SQLModel stores an Enum column by member NAME (``CALL``), not value, so
the default has to be upper case or every read of an existing row raises
LookupError. A first version of this migration wrote lower case; 0018 repairs
databases that already ran it.
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
            sa.Column("wake_trigger", sa.String(), nullable=False, server_default="CALL"),
        )


def downgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.drop_column("wake_trigger")
