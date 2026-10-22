"""Pydantic schema for the ``services.yaml`` file.

This is the on-disk (SAVED) shape of the registry. The DB holds the LIVE
version — the loader below reconciles YAML → DB on boot; the saver serialises
DB → YAML when the operator hits *Save* in the dashboard.

Anything the operator can edit through the dashboard MUST be representable
here, otherwise a save round-trip would drop it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestrator.domain.enums import BackupMode, GuestKind, ProbeKind, WakeTrigger


def _collapse_slugs(value: list[str]) -> list[str]:
    """Strip + de-duplicate a slug list, keeping first position, so a YAML that
    names a service twice hashes equal to the DB copy (no phantom drift)."""
    out: list[str] = []
    for item in value:
        slug = str(item).strip()
        if slug and slug not in out:
            out.append(slug)
    return out


class NodeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    always_on: bool = False
    power_mgr_target: str = ""
    notes: str = ""
    #: See Node.address - where the node's own Proxmox API answers.
    address: str = ""
    #: See Node.wake_on_hit_enabled — a safety rail for the TrafficCollector
    #: "wake on hit" path, not a general wake toggle.
    wake_on_hit_enabled: bool = True


class BackupPolicySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    schedule_cron: str = ""
    mode: BackupMode = BackupMode.SNAPSHOT
    retention: dict[str, Any] = Field(default_factory=dict)
    targets: dict[str, Any] = Field(default_factory=dict)
    #: Service slugs that must be up before any backup under this policy. See
    #: BackupPolicy.requires and docs/backup_wake.md.
    requires: list[str] = Field(default_factory=list)
    #: Wake the services being backed up first. See BackupPolicy.wake_before_backup.
    wake_before_backup: bool = True

    _collapse_requires = field_validator("requires")(_collapse_slugs)


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
    #: See Probe.enabled. False = switched off (kept, not run). Omitted from
    #: the YAML when true.
    enabled: bool = True


class ProxyHostSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str
    upstream: str = ""
    router_provider: str = "traefik"
    extra: dict[str, Any] = Field(default_factory=dict)
    #: See ProxyHost.wake_on_hit — per-route opt-in for the TrafficCollector
    #: "wake on hit" stand-in. Defaults False (opt-in).
    wake_on_hit: bool = False
    #: See ProxyHost.entry_points / cert_resolver. Empty = not stored.
    entry_points: list[str] = Field(default_factory=list)
    cert_resolver: str = ""
    #: See ProxyHost.route_options. Empty = defaults.
    route_options: dict[str, Any] = Field(default_factory=dict)
    #: See ProxyHost.enabled. False = switched off (kept, not deleted).
    enabled: bool = True

    @field_validator("route_options", mode="before")
    @classmethod
    def _normalise_route_options(cls, value: Any) -> Any:
        # Same normalisation as the API, so a YAML spelling hashes equal to
        # the DB copy; an all-defaults mapping collapses to {}.
        from orchestrator.adapters.proxy.base import RouteOptions

        if not value:
            return {}
        if not isinstance(value, dict):
            return value
        unknown = set(value) - set(RouteOptions().to_dict())
        if unknown:
            raise ValueError(f"unknown route_options key(s): {', '.join(sorted(unknown))}")
        normal = RouteOptions.from_dict(value).to_dict()
        if normal["config_file"]:
            from orchestrator.registry.config_files import normalize_config_file

            normal["config_file"] = normalize_config_file(normal["config_file"])
        return {} if normal == RouteOptions().to_dict() else normal

    @field_validator("entry_points", mode="before")
    @classmethod
    def _clean_entry_points(cls, value: Any) -> Any:
        # Accept "web, websecure" as well as a list; trim and de-duplicate so a
        # YAML spelling hashes equal to the DB copy.
        from orchestrator.registry.proxy_validation import parse_entry_points

        if value is None:
            return []
        if isinstance(value, str | list | tuple):
            return parse_entry_points(value)
        return value

    @field_validator("cert_resolver")
    @classmethod
    def _strip_resolver(cls, value: str) -> str:
        return value.strip()


class ServiceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    description: str = ""
    #: Purely organisational — see Service.group. "" means ungrouped.
    group: str = ""
    node: str  # -> NodeSpec.name
    guest_kind: GuestKind = GuestKind.NONE
    guest_id: int | None = None
    enabled: bool = True
    # See Service.backup_excluded — a hard, non-overridable "never back this up".
    backup_excluded: bool = False
    backup_excluded_reason: str = ""
    #: See Service.wake_on_hit_enabled — a safety rail for the
    #: TrafficCollector "wake on hit" path, not a general wake toggle.
    wake_on_hit_enabled: bool = True
    #: See Service.wake_trigger - "call" (any request wakes it) or "button"
    #: (the maintenance page shows a Start button).
    wake_trigger: WakeTrigger = WakeTrigger.CALL
    #: Slugs that must be HEALTHY before this service can be judged. See
    #: Service.depends_on — the gateway case.
    depends_on: list[str] = Field(default_factory=list)
    #: Extra slugs that must be up before THIS service's backup (on top of its
    #: policy's `requires`). Not the same as depends_on — see docs/backup_wake.md.
    backup_requires: list[str] = Field(default_factory=list)
    backup_policy: str | None = None  # -> BackupPolicySpec.name
    #: The operator's manual address. See Service.ip_address — discovered
    #: addresses are runtime state and never appear in this file.
    ip_address: str = ""
    probes: list[ProbeSpec] = Field(default_factory=list)
    proxy_hosts: list[ProxyHostSpec] = Field(default_factory=list)

    _collapse_backup_requires = field_validator("backup_requires")(_collapse_slugs)

    @field_validator("ip_address", mode="before")
    @classmethod
    def _check_ip(cls, value: Any) -> str:
        from orchestrator.registry.ip_address import validate_ip_address

        return validate_ip_address(value)


class RegistryFile(BaseModel):
    """Top-level shape of ``services.yaml``."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    nodes: list[NodeSpec] = Field(default_factory=list)
    backup_policies: list[BackupPolicySpec] = Field(default_factory=list)
    services: list[ServiceSpec] = Field(default_factory=list)
