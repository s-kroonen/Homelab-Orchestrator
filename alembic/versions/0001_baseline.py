"""baseline — create all phase 1 tables

Revision ID: 0001_baseline
Revises:
Create Date: 2026-01-01 00:00:00

Creates every table the orchestrator uses in phase 1. Later phases add
columns / indices via additional migrations. All migrations must be reversible
so a rollback can ``alembic downgrade``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel  # noqa: F401  (models use sqlmodel.AutoString etc.)
from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- Registry (YAML-owned) ------------------------------------------
    op.create_table(
        "node",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("always_on", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("power_mgr_target", sa.String(), nullable=False, server_default=""),
        sa.Column("notes", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_node_name"),
    )
    op.create_index("ix_node_name", "node", ["name"])

    op.create_table(
        "backup_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("schedule_cron", sa.String(), nullable=False, server_default=""),
        sa.Column("mode", sa.String(), nullable=False, server_default="snapshot"),
        sa.Column("retention", sa.JSON(), nullable=False),
        sa.Column("targets", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_backup_policy_name"),
    )
    op.create_index("ix_backup_policy_name", "backup_policy", ["name"])

    op.create_table(
        "service",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("guest_kind", sa.String(), nullable=False, server_default="none"),
        sa.Column("guest_id", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("node_id", sa.Integer(), nullable=True),
        sa.Column("backup_policy_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["node_id"], ["node.id"], name="fk_service_node", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["backup_policy_id"],
            ["backup_policy.id"],
            name="fk_service_backup_policy",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("slug", name="uq_service_slug"),
    )
    op.create_index("ix_service_slug", "service", ["slug"])

    op.create_table(
        "probe",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("timeout_s", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"], ["service.id"], name="fk_probe_service", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_probe_service_id", "probe", ["service_id"])

    op.create_table(
        "proxy_host",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("hostname", sa.String(), nullable=False),
        sa.Column("upstream", sa.String(), nullable=False, server_default=""),
        sa.Column("router_provider", sa.String(), nullable=False, server_default="traefik"),
        sa.Column("extra", sa.JSON(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["service.id"],
            name="fk_proxy_host_service",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_proxy_host_hostname", "proxy_host", ["hostname"])

    # ---- Admins + passkeys (DB-only, NOT rehydrated from YAML) -----------
    op.create_table(
        "admin",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("username", name="uq_admin_username"),
    )
    op.create_index("ix_admin_username", "admin", ["username"])

    op.create_table(
        "webauthn_credential",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("admin_id", sa.Integer(), nullable=False),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transports", sa.JSON(), nullable=False),
        sa.Column("aaguid", sa.String(), nullable=False, server_default=""),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["admin_id"], ["admin.id"], name="fk_credential_admin", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("credential_id", name="uq_credential_id"),
    )
    op.create_index("ix_webauthn_credential_credential_id", "webauthn_credential", ["credential_id"])

    # ---- Runtime state (never touched by YAML reconcile) -----------------
    op.create_table(
        "pipeline_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("correlation_id", sa.String(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"], ["service.id"], name="fk_run_service", ondelete="SET NULL"
        ),
    )
    op.create_index("ix_pipeline_run_correlation_id", "pipeline_run", ["correlation_id"])
    op.create_index("ix_pipeline_run_status", "pipeline_run", ["status"])
    op.create_index("ix_pipeline_run_started_at", "pipeline_run", ["started_at"])

    op.create_table(
        "backup_record",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("service_slug", sa.String(), nullable=False),
        sa.Column("policy_id", sa.Integer(), nullable=True),
        sa.Column("pipeline_run_id", sa.Integer(), nullable=True),
        sa.Column("pbs_snapshot_id", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("protected", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"], ["service.id"], name="fk_backup_service", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["backup_policy.id"], name="fk_backup_policy", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["pipeline_run.id"],
            name="fk_backup_pipeline_run",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_backup_record_service_slug", "backup_record", ["service_slug"])
    op.create_index("ix_backup_record_pbs_snapshot_id", "backup_record", ["pbs_snapshot_id"])
    op.create_index("ix_backup_record_created_at", "backup_record", ["created_at"])
    op.create_index("ix_backup_record_protected", "backup_record", ["protected"])

    op.create_table(
        "service_state",
        sa.Column("service_id", sa.Integer(), primary_key=True),
        sa.Column("last_verdict", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("last_verdict_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_probe_results", sa.JSON(), nullable=False),
        sa.Column("last_backup_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.String(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(
            ["service_id"], ["service.id"], name="fk_state_service", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["last_backup_id"],
            ["backup_record.id"],
            name="fk_state_last_backup",
            ondelete="SET NULL",
        ),
    )

    op.create_table(
        "node_state",
        sa.Column("node_id", sa.Integer(), primary_key=True),
        sa.Column("power_state", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("power_state_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hold_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_hold_reasons", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["node_id"], ["node.id"], name="fk_state_node", ondelete="CASCADE"
        ),
    )

    op.create_table(
        "audit_entry",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False, server_default=""),
        sa.Column("correlation_id", sa.String(), nullable=True),
        sa.Column("result", sa.String(), nullable=False, server_default="ok"),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_entry_actor", "audit_entry", ["actor"])
    op.create_index("ix_audit_entry_action", "audit_entry", ["action"])
    op.create_index("ix_audit_entry_correlation_id", "audit_entry", ["correlation_id"])
    op.create_index("ix_audit_entry_created_at", "audit_entry", ["created_at"])

    op.create_table(
        "pending_approval",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=False, server_default=""),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.UniqueConstraint("token", name="uq_approval_token"),
    )
    op.create_index("ix_pending_approval_token", "pending_approval", ["token"])
    op.create_index("ix_pending_approval_status", "pending_approval", ["status"])

    op.create_table(
        "state_snapshot_marker",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_state_snapshot_marker_created_at", "state_snapshot_marker", ["created_at"])

    op.create_table(
        "setting",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("setting")
    op.drop_index("ix_state_snapshot_marker_created_at", table_name="state_snapshot_marker")
    op.drop_table("state_snapshot_marker")
    op.drop_index("ix_pending_approval_status", table_name="pending_approval")
    op.drop_index("ix_pending_approval_token", table_name="pending_approval")
    op.drop_table("pending_approval")
    op.drop_index("ix_audit_entry_created_at", table_name="audit_entry")
    op.drop_index("ix_audit_entry_correlation_id", table_name="audit_entry")
    op.drop_index("ix_audit_entry_action", table_name="audit_entry")
    op.drop_index("ix_audit_entry_actor", table_name="audit_entry")
    op.drop_table("audit_entry")
    op.drop_table("node_state")
    op.drop_table("service_state")
    op.drop_index("ix_backup_record_protected", table_name="backup_record")
    op.drop_index("ix_backup_record_created_at", table_name="backup_record")
    op.drop_index("ix_backup_record_pbs_snapshot_id", table_name="backup_record")
    op.drop_index("ix_backup_record_service_slug", table_name="backup_record")
    op.drop_table("backup_record")
    op.drop_index("ix_pipeline_run_started_at", table_name="pipeline_run")
    op.drop_index("ix_pipeline_run_status", table_name="pipeline_run")
    op.drop_index("ix_pipeline_run_correlation_id", table_name="pipeline_run")
    op.drop_table("pipeline_run")
    op.drop_index("ix_webauthn_credential_credential_id", table_name="webauthn_credential")
    op.drop_table("webauthn_credential")
    op.drop_index("ix_admin_username", table_name="admin")
    op.drop_table("admin")
    op.drop_index("ix_proxy_host_hostname", table_name="proxy_host")
    op.drop_table("proxy_host")
    op.drop_index("ix_probe_service_id", table_name="probe")
    op.drop_table("probe")
    op.drop_index("ix_service_slug", table_name="service")
    op.drop_table("service")
    op.drop_index("ix_backup_policy_name", table_name="backup_policy")
    op.drop_table("backup_policy")
    op.drop_index("ix_node_name", table_name="node")
    op.drop_table("node")
