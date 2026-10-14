"""add wake-on-hit toggles — node/service/proxy_host gate for the
TrafficCollector-driven automatic wake stand-in

Revision ID: 0006_wake_on_hit
Revises: 0005_traffic_sample
Create Date: 2026-10-14 00:00:00

The owner has no write access to Traefik's own config yet (it lives on a
different host — read-only API access only, see docs/proxy_hosts.md), so
the wake trigger that ``POST /wake/{service_slug}`` was designed for
(Traefik's errors middleware calling it on behalf of a visitor hitting a
down backend — see ``web/routers/wake.py``) cannot be wired up on the
Traefik side yet. As a stand-in, ``TrafficCollector`` (which already polls
Traefik's ``/metrics`` every ``TRAEFIK_METRICS_POLL_INTERVAL_S``) can notice
"traffic hit a ProxyHost whose service is down" itself and fire the same
wake pipeline — slower than a real Traefik-side trigger by up to one poll
interval, but usable today.

Three independent, AND-gated booleans give fine-grained control over where
this NEW automatic path is allowed to act, without touching the existing
unconditional manual wake button, CLI wake command or ``POST
/wake/{slug}`` API (those are untouched by all three flags):

* ``proxy_host.wake_on_hit`` — the actual per-route opt-in: does traffic to
  this hostname count as a wake trigger at all. Defaults **False**: this
  behaviour did not exist before, so upgrading an existing install must
  never start auto-waking a service nobody asked for.
* ``service.wake_on_hit_enabled`` / ``node.wake_on_hit_enabled`` — safety
  rails layered ON TOP of that per-host opt-in, not independent opt-ins of
  their own. Defaulting them to True means turning on ``wake_on_hit`` for a
  proxy host is enough by itself (the common case); an operator who wants a
  specific service, or everything on a specific node, exempted even though
  some proxy host opts in flips the relevant rail off instead. A node with
  wake-on-hit disabled blocks every service on it via this path, regardless
  of that service's or its proxy host's own setting — same for a service
  with it disabled.

Additive with server defaults, so safe on a populated database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_wake_on_hit"
down_revision: str | None = "0005_traffic_sample"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("node") as batch:
        batch.add_column(
            sa.Column("wake_on_hit_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
    with op.batch_alter_table("service") as batch:
        batch.add_column(
            sa.Column("wake_on_hit_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
    with op.batch_alter_table("proxy_host") as batch:
        batch.add_column(
            sa.Column("wake_on_hit", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("proxy_host") as batch:
        batch.drop_column("wake_on_hit")
    with op.batch_alter_table("service") as batch:
        batch.drop_column("wake_on_hit_enabled")
    with op.batch_alter_table("node") as batch:
        batch.drop_column("wake_on_hit_enabled")
