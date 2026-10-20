"""service.ip_address + discovered-IP runtime state on service_state

Revision ID: 0014_service_ip
Revises: 0013_proxy_host_enabled
Create Date: 2026-10-20 12:00:00

Services are linked to an IP address so a proxy host can use it as its
upstream and a ping probe can target it:

* ``service.ip_address`` — the operator's MANUAL address (YAML-owned, "" =
  none). It always wins over a discovered one.
* ``service_state.discovered_ip`` / ``ip_source`` / ``mac_addresses`` /
  ``ip_discovered_at`` / ``ip_discovery_error`` — what the IP discovery job
  found (guest agent, LXC config/interfaces, ARP, DHCP leases). Runtime only:
  never written to services.yaml, so discovery never makes the registry dirty.

Additive with server defaults, so safe on a populated database.
See docs/service_ips.md.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_service_ip"
down_revision: str | None = "0013_proxy_host_enabled"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("service") as batch:
        batch.add_column(
            sa.Column("ip_address", sa.String(), nullable=False, server_default=""),
        )
    with op.batch_alter_table("service_state") as batch:
        batch.add_column(
            sa.Column("discovered_ip", sa.String(), nullable=False, server_default=""),
        )
        batch.add_column(sa.Column("ip_source", sa.String(), nullable=False, server_default=""))
        batch.add_column(
            sa.Column("mac_addresses", sa.JSON(), nullable=False, server_default="[]"),
        )
        batch.add_column(
            sa.Column("ip_discovered_at", sa.DateTime(timezone=True), nullable=True),
        )
        batch.add_column(
            sa.Column("ip_discovery_error", sa.String(), nullable=False, server_default=""),
        )


def downgrade() -> None:
    with op.batch_alter_table("service_state") as batch:
        batch.drop_column("ip_discovery_error")
        batch.drop_column("ip_discovered_at")
        batch.drop_column("mac_addresses")
        batch.drop_column("ip_source")
        batch.drop_column("discovered_ip")
    with op.batch_alter_table("service") as batch:
        batch.drop_column("ip_address")
