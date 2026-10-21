"""proxy_host.pushed_sig - fingerprint of what was last pushed to Traefik Manager

Revision ID: 0015_proxy_host_pushed_sig
Revises: 0014_service_ip
Create Date: 2026-10-21 12:00:00

Proxy-host edits now change the database only; Traefik Manager is written on
"Save to YAML" or "Push now". To know what is still pending, each host
remembers a fingerprint of everything last pushed for it (hostname, upstream,
entry points, cert resolver, route options, enabled). Empty = unknown (never
pushed from here), which is what every existing row starts as - the first
status read compares such a host with what Traefik Manager really has instead
of assuming it differs.

Additive with a server default, so safe on a populated database.
See docs/proxy_hosts.md "Pushing to Traefik Manager".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_proxy_host_pushed_sig"
down_revision: str | None = "0014_service_ip"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("proxy_host") as batch:
        batch.add_column(sa.Column("pushed_sig", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("proxy_host") as batch:
        batch.drop_column("pushed_sig")
