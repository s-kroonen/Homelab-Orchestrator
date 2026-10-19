"""proxy_host.entry_points + cert_resolver

Revision ID: 0011_proxy_host_entrypoints
Revises: 0010_backup_wake_prereqs
Create Date: 2026-10-19 12:00:00

A live-managed proxy host used to be written to Traefik Manager without any
entry points, which Traefik Manager turned into its own default (`https`) -
an entry point most setups do not have, leaving the router flagged as broken.
The chosen entry points and certificate resolver are now stored per host.

Additive with server defaults, so safe on a populated database: existing rows
get an empty list / empty string, meaning "not stored yet" - the edit form
then pre-selects from what Traefik Manager really has on the route.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_proxy_host_entrypoints"
down_revision: str | None = "0010_backup_wake_prereqs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("proxy_host") as batch:
        batch.add_column(
            sa.Column("entry_points", sa.JSON(), nullable=False, server_default="[]"),
        )
        batch.add_column(
            sa.Column("cert_resolver", sa.String(), nullable=False, server_default=""),
        )


def downgrade() -> None:
    with op.batch_alter_table("proxy_host") as batch:
        batch.drop_column("cert_resolver")
        batch.drop_column("entry_points")
