"""Pydantic schema for the ``services.yaml`` file.

This is the on-disk (SAVED) shape of the registry. The DB holds the LIVE
version — the loader below reconciles YAML → DB on boot; the saver serialises
DB → YAML when the operator hits *Save* in the dashboard.

Anything the operator can edit through the dashboard MUST be representable
here, otherwise a save round-trip would drop it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.domain.enums import BackupMode, GuestKind, ProbeKind


class NodeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    always_on: bool = False
    power_mgr_target: str = ""
    notes: str = ""


class BackupPolicySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    schedule_cron: str = ""
    mode: BackupMode = BackupMode.SNAPSHOT
    retention: dict[str, Any] = Field(default_factory=dict)
    targets: dict[str, Any] = Field(default_factory=dict)


class ProbeSpec(BaseModel):
    """A probe entry in YAML. ``config`` is validated per-``kind`` at use time
    against :mod:`orchestrator.domain.schemas` — here we accept any dict so the
    YAML shape stays uniform."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: ProbeKind
    required: bool = True
    timeout_s: int = 15
    order: int = 0
    config: dict[str, Any] = Field(default_factory=dict)


class ProxyHostSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str
    upstream: str = ""
    router_provider: str = "traefik"
    extra: dict[str, Any] = Field(default_factory=dict)


class ServiceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    description: str = ""
    node: str  # -> NodeSpec.name
    guest_kind: GuestKind = GuestKind.NONE
    guest_id: int | None = None
    enabled: bool = True
    # See Service.backup_excluded — a hard, non-overridable "never back this up".
    backup_excluded: bool = False
    backup_excluded_reason: str = ""
    #: Slugs that must be HEALTHY before this service can be judged. See
    #: Service.depends_on — the gateway case.
    depends_on: list[str] = Field(default_factory=list)
    backup_policy: str | None = None  # -> BackupPolicySpec.name
    probes: list[ProbeSpec] = Field(default_factory=list)
    proxy_hosts: list[ProxyHostSpec] = Field(default_factory=list)


class RegistryFile(BaseModel):
    """Top-level shape of ``services.yaml``."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    nodes: list[NodeSpec] = Field(default_factory=list)
    backup_policies: list[BackupPolicySpec] = Field(default_factory=list)
    services: list[ServiceSpec] = Field(default_factory=list)
