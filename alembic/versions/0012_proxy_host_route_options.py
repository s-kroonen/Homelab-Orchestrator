"""proxy_host.route_options

Revision ID: 0012_proxy_host_route_options
Revises: 0011_proxy_host_entrypoints
Create Date: 2026-10-19 18:00:00

A live-managed route used to be written with Traefik Manager's defaults for
everything not chosen here, including `passHostHeader: false`, which made the
proxy send the upstream's own LAN address as `Host`; backends that build
redirects from it (Proxmox, PBS, most web UIs) bounced the browser to that raw
IP. The route options (host header, upstream TLS verify, wildcard certificate
domains, HTTP -> HTTPS redirect router) are now stored per host.

Additive with a server default, so safe on a populated database: an empty
object means "use the defaults", which is `passHostHeader: true`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_proxy_host_route_options"
down_revision: str | None = "0011_proxy_host_entrypoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("proxy_host") as batch:
        batch.add_column(
            sa.Column("route_options", sa.JSON(), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    with op.batch_alter_table("proxy_host") as batch:
        batch.drop_column("route_options")
