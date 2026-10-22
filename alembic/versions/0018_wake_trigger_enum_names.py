"""repair service.wake_trigger values written in lower case by 0017 (first draft)

Revision ID: 0018_wake_trigger_enum_names
Revises: 0017_service_wake_trigger
Create Date: 2026-10-22 14:00:00

SQLModel stores an Enum column by member NAME. The first version of 0017 gave
``wake_trigger`` a lower-case default (``call``), so every pre-existing service
row held a value SQLAlchemy cannot map back to ``WakeTrigger`` and reading any
service raised ``LookupError`` (500 on the dashboard, proxy hosts and the
maintenance page). This rewrites those rows and the column default to the
member names. Idempotent: a database that never had the bad value is untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_wake_trigger_enum_names"
down_revision: str | None = "0017_service_wake_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    for old, new in (("call", "CALL"), ("button", "BUTTON")):
        bind.execute(
            sa.text("UPDATE service SET wake_trigger = :new WHERE wake_trigger = :old"),
            {"old": old, "new": new},
        )
    with op.batch_alter_table("service") as batch:
        batch.alter_column(
            "wake_trigger",
            existing_type=sa.String(),
            existing_nullable=False,
            server_default="CALL",
        )


def downgrade() -> None:
    # Nothing to undo: the repaired values are what the ORM always wrote.
    pass
