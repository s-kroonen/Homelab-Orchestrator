"""probe.enabled - switch a probe off without deleting it

Revision ID: 0016_probe_enabled
Revises: 0015_proxy_host_pushed_sig
Create Date: 2026-10-21 18:00:00

A disabled proxy host is not served, so the auto-created ``traefik:<hostname>``
probe would fail for no reason. Deleting the probe would lose the operator's
``required`` / timeout settings, so a probe gets an ``enabled`` flag instead:
a disabled probe is not run and never affects the verdict, but keeps all of
its settings for when it is switched back on.

Additive with a server default of true, so every existing probe keeps running.
See docs/architecture.md and docs/proxy_hosts.md.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_probe_enabled"
down_revision: str | None = "0015_proxy_host_pushed_sig"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("probe") as batch:
        batch.add_column(
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
    # Hosts that are already disabled: their auto-created probe follows them.
    op.get_bind().execute(
        sa.text(
            "UPDATE probe SET enabled = :off WHERE name IN ("
            "SELECT 'traefik:' || proxy_host.hostname FROM proxy_host "
            "WHERE proxy_host.enabled = :off AND proxy_host.service_id = probe.service_id)"
        ),
        {"off": False},
    )


def downgrade() -> None:
    with op.batch_alter_table("probe") as batch:
        batch.drop_column("enabled")
