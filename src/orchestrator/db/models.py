"""SQLModel table definitions — the state store for the orchestrator.

Design rules for this schema:

* **YAML-owned vs DB-owned.**  The registry tables (:class:`Node`, :class:`Service`,
  :class:`Probe`, :class:`BackupPolicy`, :class:`ProxyHost`) are reconciled from the
  YAML file on every boot.  Admins + passkeys + all runtime tables are DB-only,
  otherwise a reload would lock the operator out and destroy history.
* **JSON columns** hold discriminated-union / bag-of-fields data (probe configs,
  step timings, approval payloads).  Validation is enforced at the Pydantic
  layer, not by SQLite.
* **FK ON DELETE policies** protect history: removing a service from YAML must
  not orphan valid backup rows.  Runtime state (``ServiceState``) is CASCADE.
* **Timestamps** are timezone-aware UTC via ``datetime.now(UTC)``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, ForeignKey
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel

from orchestrator.domain.enums import (
    ApprovalKind,
    ApprovalStatus,
    AuditResult,
    BackupMode,
    GuestKind,
    HealthState,
    PipelineKind,
    PipelineStatus,
    PowerState,
    ProbeKind,
    RouterProvider,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Registry — YAML-owned. Reconciled from services.yaml on boot.
# ---------------------------------------------------------------------------


class Node(SQLModel, table=True):
    """A Proxmox host. ``power_mgr_target`` is opaque to us; the power adapter
    interprets it (an MQTT topic suffix, a hostname, an IPMI target, whatever)."""

    __tablename__ = "node"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    always_on: bool = Field(default=False)
    power_mgr_target: str = ""
    notes: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class BackupPolicy(SQLModel, table=True):
    __tablename__ = "backup_policy"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    schedule_cron: str = ""
    mode: BackupMode = Field(default=BackupMode.SNAPSHOT)
    # retention: {"keep_daily": 7, "keep_monthly": 6, "protect_latest_verified": true}
    retention: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # targets: {"services": ["example-media", "example-cloud"]}
    targets: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Service(SQLModel, table=True):
    __tablename__ = "service"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    name: str
    description: str = ""
    guest_kind: GuestKind = Field(default=GuestKind.NONE)
    guest_id: int | None = None
    enabled: bool = Field(default=True)
    node_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("node.id", ondelete="RESTRICT")),
    )
    backup_policy_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("backup_policy.id", ondelete="SET NULL")),
    )
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Probe(SQLModel, table=True):
    """Per-service check descriptor. ``config`` is validated at app layer per ``kind``.

    ``required=False`` marks the probe as diagnostic-only: its result is recorded and
    surfaced on the dashboard but does not affect the aggregated verdict / gate.
    """

    __tablename__ = "probe"

    id: int | None = Field(default=None, primary_key=True)
    service_id: int = Field(
        sa_column=Column(ForeignKey("service.id", ondelete="CASCADE"), nullable=False),
    )
    name: str
    kind: ProbeKind
    required: bool = Field(default=True)
    timeout_s: int = Field(default=15)
    order: int = Field(default=0)
    config: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class ProxyHost(SQLModel, table=True):
    """Traefik / Pangolin / NPM route observed for a service.

    Seam introduced from day 1 so phases 5 and 7 have somewhere to write. In
    phase 1 this table is created but not populated.
    """

    __tablename__ = "proxy_host"

    id: int | None = Field(default=None, primary_key=True)
    service_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("service.id", ondelete="SET NULL")),
    )
    hostname: str = Field(index=True)
    upstream: str = ""
    router_provider: RouterProvider = Field(default=RouterProvider.TRAEFIK)
    extra: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    last_seen_at: datetime | None = None


# ---------------------------------------------------------------------------
# Admins + passkeys — DB-only. NOT rehydrated from YAML, otherwise reloading
# services.yaml would lock the operator out.
# ---------------------------------------------------------------------------


class Admin(SQLModel, table=True):
    __tablename__ = "admin"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    display_name: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    disabled_at: datetime | None = None


class WebAuthnCredential(SQLModel, table=True):
    __tablename__ = "webauthn_credential"

    id: int | None = Field(default=None, primary_key=True)
    admin_id: int = Field(
        sa_column=Column(ForeignKey("admin.id", ondelete="CASCADE"), nullable=False),
    )
    credential_id: bytes = Field(index=True, unique=True)
    public_key: bytes
    sign_count: int = Field(default=0)
    transports: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    aaguid: str = ""
    label: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    last_used_at: datetime | None = None


# ---------------------------------------------------------------------------
# Runtime state — never touched by YAML reconcile.
# ---------------------------------------------------------------------------


class ServiceState(SQLModel, table=True):
    """One row per service; rewritten by the health engine."""

    __tablename__ = "service_state"

    service_id: int = Field(
        sa_column=Column(
            ForeignKey("service.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    last_verdict: HealthState = Field(default=HealthState.UNKNOWN)
    last_verdict_at: datetime | None = None
    last_probe_results: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON),
    )
    last_backup_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("backup_record.id", ondelete="SET NULL")),
    )
    notes: str = ""


class NodeState(SQLModel, table=True):
    """One row per node. ``hold_count`` prevents the usage-based shutdown from
    firing while a wake or backup is in progress; ``current_hold_reasons`` records
    who is holding it and why so a stuck hold can be diagnosed."""

    __tablename__ = "node_state"

    node_id: int = Field(
        sa_column=Column(
            ForeignKey("node.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    power_state: PowerState = Field(default=PowerState.UNKNOWN)
    power_state_at: datetime | None = None
    last_seen_at: datetime | None = None
    hold_count: int = Field(default=0)
    current_hold_reasons: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON),
    )


class PipelineRun(SQLModel, table=True):
    """Every wake / backup / restore pipeline run. Used for the dashboard timeline,
    resume-after-restart logic, and audit joins."""

    __tablename__ = "pipeline_run"

    id: int | None = Field(default=None, primary_key=True)
    kind: PipelineKind
    correlation_id: str = Field(index=True)
    service_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("service.id", ondelete="SET NULL")),
    )
    status: PipelineStatus = Field(default=PipelineStatus.QUEUED, index=True)
    started_at: datetime = Field(default_factory=_utcnow, index=True)
    finished_at: datetime | None = None
    error: str | None = None
    # steps: list of {"name": str, "started_at": iso, "finished_at": iso, "status": str, "detail": {...}}
    steps: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))


class BackupRecord(SQLModel, table=True):
    __tablename__ = "backup_record"

    id: int | None = Field(default=None, primary_key=True)
    service_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("service.id", ondelete="SET NULL")),
    )
    # Denormalised so the record still identifies its origin after a service is
    # removed from YAML and cascaded out of ``service``.
    service_slug: str = Field(index=True)
    policy_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("backup_policy.id", ondelete="SET NULL")),
    )
    pipeline_run_id: int | None = Field(
        default=None,
        sa_column=Column(ForeignKey("pipeline_run.id", ondelete="SET NULL")),
    )
    pbs_snapshot_id: str = Field(index=True)  # e.g. "vm/9001/2026-08-24T02:00:00Z"
    size_bytes: int | None = None
    verified: bool = Field(default=False)
    verified_at: datetime | None = None
    protected: bool = Field(default=False, index=True)  # the known-good pin
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class AuditEntry(SQLModel, table=True):
    """Append-only. Insertions only — never updated or deleted from application code."""

    __tablename__ = "audit_entry"

    id: int | None = Field(default=None, primary_key=True)
    actor: str = Field(index=True)  # username or "system"
    action: str = Field(index=True)  # e.g. "wake.request", "backup.gate.failed"
    target: str = ""  # free-form: "service:example-media"
    correlation_id: str | None = Field(default=None, index=True)
    result: AuditResult = Field(default=AuditResult.OK)
    details: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class PendingApproval(SQLModel, table=True):
    """Two-phase confirmation token for high-risk actions.

    The token is what an approve-link carries; ``payload`` is the *exact* action
    that will be executed — never re-derived at approval time."""

    __tablename__ = "pending_approval"

    id: int | None = Field(default=None, primary_key=True)
    kind: ApprovalKind
    token: str = Field(index=True, unique=True)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    requested_by: str = ""
    requested_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None
    status: ApprovalStatus = Field(default=ApprovalStatus.PENDING, index=True)


class StateSnapshotMarker(SQLModel, table=True):
    """Bookkeeping for the periodic SQLite → NFS dump job."""

    __tablename__ = "state_snapshot_marker"

    id: int | None = Field(default=None, primary_key=True)
    kind: str  # "pre_migration", "scheduled", "manual"
    path: str
    size_bytes: int | None = None
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class Setting(SQLModel, table=True):
    """Tiny key/value store for singleton metadata (last-loaded YAML hash, etc.).

    Used by the registry loader to detect drift between the DB (live) and the
    on-disk YAML (saved), so the dashboard can show an unsaved-changes badge."""

    __tablename__ = "setting"

    key: str = Field(primary_key=True)
    value: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=_utcnow)


# Well-known Setting keys, so callers don't stringly-type them.
SETTING_LAST_YAML_LOAD = "registry.last_yaml_load"  # {"path", "sha256", "at"}
SETTING_LAST_YAML_SAVE = "registry.last_yaml_save"  # {"path", "sha256", "at", "by"}
