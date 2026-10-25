"""service.deploy: how an application is redeployed and what guards it

Revision ID: 0025_service_deploy
Revises: 0024_push_subscription
Create Date: 2026-10-24 12:00:00

Additive: every existing service starts with no deploy method ({}).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_service_deploy"
down_revision: str | None = "0024_push_subscription"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.add_column(sa.Column("deploy", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.drop_column("deploy")
