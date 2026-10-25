"""mcp: connectors, grants (tokens), authorisation codes and the call history

Revision ID: 0026_mcp
Revises: 0025_service_deploy
Create Date: 2026-10-25 12:00:00

Additive: four new tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_mcp"
down_revision: str | None = "0025_service_deploy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_client",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False, server_default=""),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="dcr"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_mcp_client_client_id", "mcp_client", ["client_id"], unique=True)

    op.create_table(
        "mcp_grant",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False, server_default="oauth"),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("client_id", sa.String(), nullable=False, server_default=""),
        sa.Column("client_name", sa.String(), nullable=False, server_default=""),
        sa.Column("admin_username", sa.String(), nullable=False, server_default=""),
        sa.Column("role", sa.String(), nullable=False, server_default="viewer"),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("confirm_writes", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("access_hash", sa.String(), nullable=False, server_default=""),
        sa.Column("access_expires_at", sa.DateTime(), nullable=True),
        sa.Column("refresh_hash", sa.String(), nullable=False, server_default=""),
        sa.Column("prev_refresh_hash", sa.String(), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_mcp_grant_access_hash", "mcp_grant", ["access_hash"])
    op.create_index("ix_mcp_grant_refresh_hash", "mcp_grant", ["refresh_hash"])

    op.create_table(
        "mcp_auth_code",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("redirect_uri", sa.String(), nullable=False),
        sa.Column("code_challenge", sa.String(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("role", sa.String(), nullable=False, server_default="viewer"),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("confirm_writes", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("admin_username", sa.String(), nullable=False, server_default=""),
        sa.Column("resource", sa.String(), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_mcp_auth_code_code_hash", "mcp_auth_code", ["code_hash"], unique=True)

    op.create_table(
        "mcp_call",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("grant_id", sa.Integer(), nullable=True),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("client_name", sa.String(), nullable=False, server_default=""),
        sa.Column("role", sa.String(), nullable=False, server_default=""),
        sa.Column("tool", sa.String(), nullable=False, server_default=""),
        sa.Column("risk", sa.String(), nullable=False, server_default="read"),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="ok"),
        sa.Column("summary", sa.String(), nullable=False, server_default=""),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("approval_token", sa.String(), nullable=True),
        sa.Column("decided_by", sa.String(), nullable=True),
    )
    op.create_index("ix_mcp_call_at", "mcp_call", ["at"])
    op.create_index("ix_mcp_call_grant_id", "mcp_call", ["grant_id"])
    op.create_index("ix_mcp_call_tool", "mcp_call", ["tool"])
    op.create_index("ix_mcp_call_approval_token", "mcp_call", ["approval_token"])


def downgrade() -> None:
    op.drop_table("mcp_call")
    op.drop_table("mcp_auth_code")
    op.drop_table("mcp_grant")
    op.drop_table("mcp_client")
