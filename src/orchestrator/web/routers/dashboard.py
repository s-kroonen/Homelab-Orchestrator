"""Dashboard + registry API.

List services, show the unsaved-changes badge state, and trigger Save /
Reset. Phase 7 gates all of this behind a logged-in admin session — see
``web/auth.py``.

The registry CRUD endpoints below (services, nodes, probes) only ever touch
the DB. ``services.yaml`` stays the SAVED copy: an edit here shows up as the
dashboard's unsaved-changes badge, and the existing Save / Reset endpoints
round-trip it to / from YAML through ``registry/loader.py``. Nothing in this
module writes YAML itself.

Validation is deliberately stricter than the DB schema: anything accepted
here must survive a Save → Reset round trip, so references the YAML loader
would reject (unknown node, unknown depends_on slug, a dependency cycle) are
refused up front with a 400 rather than being discovered at the next reload.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlmodel import Session, col, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.audit import log as audit
from orchestrator.config import get_settings
from orchestrator.db.models import (
    BackupPolicy,
    HealthScanSample,
    Node,
    PipelineRun,
    Probe,
    ProxyHost,
    RecoveryState,
    Service,
    ServiceState,
)
from orchestrator.domain.enums import (
    BackupGate,
    GuestKind,
    PipelineKind,
    PipelineStatus,
    ProbeKind,
    WakeTrigger,
)
from orchestrator.domain.schemas import HttpProbeConfig, PingProbeConfig, TcpProbeConfig
from orchestrator.health.engine import HealthEngine
from orchestrator.ipdiscovery.discovery import IpDiscoverer, effective_ip, ip_view
from orchestrator.pipelines.wake import wake_is_unsupported
from orchestrator.registry.backup_plan import clean_slug_list, requires_problems_from_db
from orchestrator.registry.ip_address import validate_ip_address
from orchestrator.registry.loader import (
    _find_dependency_cycle,
    compute_registry_diff,
    reload_registry_from_yaml,
    save_registry_to_yaml,
)
from orchestrator.registry.probe_specs import PROBE_SPECS
from orchestrator.registry.proxy_push import move_to_configured_file
from orchestrator.registry.push import PushResult, apply_all, get_pushers, status_all, summarize
from orchestrator.registry.tree import MAX_DEPTH, TreeIndex, would_cycle
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep
from orchestrator.web.proxy_catalog import get_catalog

router = APIRouter(prefix="/api", tags=["dashboard"], dependencies=[Depends(require_admin)])

#: Slugs end up in URLs (``/services/{slug}``, ``/api/registry/services/{slug}``)
#: and in other services' ``depends_on`` lists, so keep them boring.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
#: Transport types the health engine actually implements
#: (``health/transports/{local,ssh,host_agent}.py``).
_TRANSPORT_TYPES = ("host_agent", "ssh", "local")
#: Network probes validate their config with these models at run time
#: (``health/probes/network.py``), so a config they would reject is refused here.
_STRICT_CONFIG_MODELS: dict[ProbeKind, type[BaseModel]] = {
    ProbeKind.HTTP: HttpProbeConfig,
    ProbeKind.TCP: TcpProbeConfig,
    ProbeKind.PING: PingProbeConfig,
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
#
# Enum-valued fields (guest_kind, probe kind) are plain ``str`` on purpose and
# checked by hand: a bad value then gets a readable 400 listing the valid
# choices, instead of FastAPI's generic 422 validation dump.


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceCreate(_Body):
    slug: str
    name: str
    description: str = ""
    group: str = ""
    #: Required for a root; a sub-service (``parent`` set) takes its root's.
    node_id: int | None = None
    #: Slug of the parent: this is then a sub-service (container, add-on).
    parent: str | None = None
    controller: dict[str, Any] = Field(default_factory=dict)
    recovery: dict[str, Any] = Field(default_factory=dict)
    deploy: dict[str, Any] = Field(default_factory=dict)
    backup_gate: str = BackupGate.IGNORE.value
    guest_kind: str = GuestKind.NONE.value
    guest_id: int | None = None
    enabled: bool = True
    backup_excluded: bool = False
    backup_excluded_reason: str = ""
    backup_policy_id: int | None = None
    depends_on: list[str] = Field(default_factory=list)
    backup_requires: list[str] = Field(default_factory=list)
    ip_address: str = ""


class ServiceUpdate(_Body):
    """Partial update: only fields present in the body are touched. ``slug``
    is not editable — other services reference it by slug in ``depends_on``."""

    name: str | None = None
    description: str | None = None
    group: str | None = None
    node_id: int | None = None
    guest_kind: str | None = None
    guest_id: int | None = None
    enabled: bool | None = None
    backup_excluded: bool | None = None
    backup_excluded_reason: str | None = None
    backup_policy_id: int | None = None
    wake_on_hit_enabled: bool | None = None
    #: "call" or "button" - see Service.wake_trigger.
    wake_trigger: str | None = None
    #: Slug of the parent ("" / null = make it a root). See Service.parent_id.
    parent: str | None = None
    controller: dict[str, Any] | None = None
    recovery: dict[str, Any] | None = None
    deploy: dict[str, Any] | None = None
    #: "ignore" or "required" - see Service.backup_gate.
    backup_gate: str | None = None
    depends_on: list[str] | None = None
    backup_requires: list[str] | None = None
    #: The manual IP ("" clears it). Discovered addresses are never set here.
    ip_address: str | None = None


class NodeCreate(_Body):
    name: str
    always_on: bool = False
    power_mgr_target: str = ""
    notes: str = ""
    address: str = ""
    wake_on_hit_enabled: bool = True


class NodeUpdate(_Body):
    name: str | None = None
    always_on: bool | None = None
    power_mgr_target: str | None = None
    notes: str | None = None
    address: str | None = None
    wake_on_hit_enabled: bool | None = None


class ProbeCreate(_Body):
    name: str
    kind: str
    required: bool = True
    #: None -> the kind's default from ``PROBE_SPECS`` (what the CLI would pick).
    timeout_s: int | None = None
    #: None -> appended after the service's existing probes.
    order: int | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    #: False = created switched off (not run, never in the verdict).
    enabled: bool = True


class ProbeUpdate(_Body):
    name: str | None = None
    kind: str | None = None
    required: bool | None = None
    timeout_s: int | None = None
    order: int | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


# ---------------------------------------------------------------------------
# Serialisers + lookups
# ---------------------------------------------------------------------------


def _check_node_address(raw: str) -> str:
    """``host`` or ``host:port`` of a node's Proxmox API (no scheme or path)."""
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" in value or "/" in value or " " in value:
        raise _bad_request("address is a host or host:port, without http:// or a path")
    _, _, port = value.rpartition(":") if value.count(":") == 1 else (value, "", "")
    if port and not (port.isdigit() and 0 < int(port) < 65536):
        raise _bad_request(f"address port {port!r} is not a valid port")
    return value


def _node_dict(node: Node) -> dict[str, Any]:
    return {
        "id": node.id,
        "name": node.name,
        "always_on": node.always_on,
        "power_mgr_target": node.power_mgr_target,
        "notes": node.notes,
        "address": node.address,
        "wake_on_hit_enabled": node.wake_on_hit_enabled,
    }


def _probe_dict(probe: Probe) -> dict[str, Any]:
    return {
        "id": probe.id,
        "service_id": probe.service_id,
        "name": probe.name,
        "kind": probe.kind.value,
        "required": probe.required,
        "timeout_s": probe.timeout_s,
        "order": probe.order,
        "config": probe.config,
        "enabled": probe.enabled,
    }


def _service_probes(session: Session, service: Service) -> list[Probe]:
    return list(
        session.exec(
            select(Probe)
            .where(Probe.service_id == service.id)
            .order_by(col(Probe.order), col(Probe.name))
        ).all()
    )


def _service_proxy_hosts(session: Session, service: Service) -> list[ProxyHost]:
    return list(
        session.exec(
            select(ProxyHost)
            .where(ProxyHost.service_id == service.id)
            .order_by(col(ProxyHost.hostname))
        ).all()
    )


def _proxy_host_dict(ph: ProxyHost) -> dict[str, Any]:
    # Deliberately NOT the full web/routers/proxy_hosts.py `_serialize` shape
    # (no `live_status` — that needs an async Traefik poll this sync
    # function has no business making just to render a service page). The
    # service page's own JS calls `GET/POST/DELETE /api/proxy-hosts[/{id}]`
    # for every mutation, so this only needs enough to list and delete.
    return {
        "id": ph.id,
        "hostname": ph.hostname,
        "upstream": ph.upstream,
        "router_provider": ph.router_provider.value,
        "wake_on_hit": ph.wake_on_hit,
        "enabled": ph.enabled,
    }


def _recovery_state(session: Session, svc: Service) -> dict[str, Any] | None:
    row = session.get(RecoveryState, svc.id) if svc.id is not None else None
    if row is None or not (row.attempts or row.failing_scans):
        return None
    return {
        "attempts": row.attempts,
        "failing_scans": row.failing_scans,
        "exhausted": row.exhausted,
        "last_action": row.last_action,
        "last_result": row.last_result,
        "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else None,
    }


def _tree_info(session: Session, svc: Service) -> dict[str, Any]:
    index = TreeIndex.build(session)
    parent = index.parent(svc)
    states = {s.service_id: s.last_verdict.value for s in session.exec(select(ServiceState)).all()}
    return {
        "parent": parent.slug if parent else None,
        "root": index.root(svc).slug,
        "depth": index.depth(svc),
        "served": index.is_served(svc),
        "children": [
            {
                "id": c.id,
                "slug": c.slug,
                "name": c.name,
                "enabled": c.enabled,
                "verdict": states.get(c.id, "unknown"),
                "backup_gate": c.backup_gate.value,
            }
            for c in index.kids(svc)
        ],
    }


def service_detail(session: Session, svc: Service) -> dict[str, Any]:
    tree_info = _tree_info(session, svc)
    node = session.get(Node, svc.node_id) if svc.node_id is not None else None
    policy = (
        session.get(BackupPolicy, svc.backup_policy_id)
        if svc.backup_policy_id is not None
        else None
    )
    return {
        "id": svc.id,
        "slug": svc.slug,
        "name": svc.name,
        "description": svc.description,
        "group": svc.group,
        "enabled": svc.enabled,
        "guest_kind": svc.guest_kind.value,
        "guest_id": svc.guest_id,
        "backup_excluded": svc.backup_excluded,
        "backup_excluded_reason": svc.backup_excluded_reason,
        "wake_on_hit_enabled": svc.wake_on_hit_enabled,
        "wake_trigger": svc.wake_trigger.value,
        "parent": tree_info["parent"],
        "root": tree_info["root"],
        "depth": tree_info["depth"],
        "children": tree_info["children"],
        "served": tree_info["served"],
        "controller": dict(svc.controller or {}),
        "recovery": dict(svc.recovery or {}),
        "deploy": dict(svc.deploy or {}),
        "backup_gate": svc.backup_gate.value,
        "recovery_state": _recovery_state(session, svc),
        "backup_policy_id": svc.backup_policy_id,
        "backup_policy": policy.name if policy else None,
        "depends_on": list(svc.depends_on or []),
        "backup_requires": list(svc.backup_requires or []),
        "node_id": svc.node_id,
        "node": _node_dict(node) if node else None,
        "ip_address": svc.ip_address or "",
        # Effective / discovered address, MACs and where it came from — see
        # docs/service_ips.md. Runtime state, never part of the YAML.
        "ip": ip_view(svc, session.get(ServiceState, svc.id) if svc.id is not None else None),
        # Same condition orchestrator.pipelines.wake.WakePipeline uses to
        # raise WakeUnsupportedError, computed here so an admin can see
        # upfront which services this applies to rather than only
        # discovering it when something is actually down — see
        # docs/architecture.md.
        "wake_unsupported": wake_is_unsupported(node),
        "probes": [_probe_dict(p) for p in _service_probes(session, svc)],
        "proxy_hosts": [_proxy_host_dict(p) for p in _service_proxy_hosts(session, svc)],
        "created_at": svc.created_at.isoformat(),
        "updated_at": svc.updated_at.isoformat(),
    }


def _get_service(session: Session, slug: str) -> Service:
    svc = session.exec(select(Service).where(Service.slug == slug)).first()
    if svc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no service {slug!r}")
    return svc


def _get_node(session: Session, name: str) -> Node:
    node = session.exec(select(Node).where(Node.name == name)).first()
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no node {name!r}")
    return node


def _get_probe(session: Session, probe_id: int) -> tuple[Probe, Service]:
    probe = session.get(Probe, probe_id)
    if probe is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no probe {probe_id}")
    svc = session.get(Service, probe.service_id)
    assert svc is not None  # FK is NOT NULL + CASCADE, so a probe always has one
    return probe, svc


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _require_text(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise _bad_request(f"{field} must not be empty")
    return value


def _parse_guest_kind(raw: str) -> GuestKind:
    try:
        return GuestKind(raw)
    except ValueError:
        choices = ", ".join(k.value for k in GuestKind)
        raise _bad_request(f"unknown guest_kind {raw!r} (expected one of: {choices})") from None


def _parse_probe_kind(raw: str) -> ProbeKind:
    try:
        return ProbeKind(raw)
    except ValueError:
        choices = ", ".join(k.value for k in ProbeKind)
        raise _bad_request(f"unknown probe kind {raw!r} (expected one of: {choices})") from None


def _check_json_object(raw: Any, field: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _bad_request(f"{field} must be an object")
    return dict(raw)


def _check_recovery(raw: Any) -> dict[str, Any]:
    from orchestrator.recovery.policy import normalize_policy

    try:
        return normalize_policy(_check_json_object(raw, "recovery"))
    except ValueError as exc:
        raise _bad_request(str(exc)) from None


def _check_deploy(raw: Any, controller: dict[str, Any]) -> dict[str, Any]:
    from orchestrator.deploy.policy import check_target, normalize_deploy

    try:
        deploy = normalize_deploy(_check_json_object(raw, "deploy"))
        check_target(deploy, controller)
    except ValueError as exc:
        raise _bad_request(str(exc)) from None
    return deploy


def _check_controller(raw: Any) -> dict[str, Any]:
    from orchestrator.controllers import validate_controller

    try:
        return validate_controller(_check_json_object(raw, "controller"))
    except ValueError as exc:
        raise _bad_request(str(exc)) from None


def _parse_backup_gate(raw: str | None) -> BackupGate:
    try:
        return BackupGate(str(raw or "ignore").strip().lower())
    except ValueError:
        raise _bad_request("backup_gate must be 'ignore' or 'required'") from None


def _resolve_parent(
    session: Session, index: TreeIndex, parent_slug: str | None, *, child: Service | None
) -> Service | None:
    """The parent row for ``parent_slug`` ("" / None = no parent), validated."""
    slug = (parent_slug or "").strip()
    if not slug:
        return None
    parent = index.by_slug.get(slug)
    if parent is None:
        raise _bad_request(f"parent {slug!r} does not exist")
    if child is not None and would_cycle(index, child, parent):
        raise _bad_request(f"{slug!r} is {child.slug!r} or below it - that would be a loop")
    depth = index.depth(parent) + 1
    if child is not None:
        depth += max(
            (index.depth(d) - index.depth(child) for d in index.descendants(child)), default=0
        )
    if depth > MAX_DEPTH:
        raise _bad_request(f"services nest at most {MAX_DEPTH} levels deep")
    return parent


def _check_node_exists(session: Session, node_id: int | None) -> None:
    # ServiceSpec.node is required in YAML, so a node-less service could be
    # saved but never reloaded. Refuse it here instead.
    if node_id is None:
        raise _bad_request("node_id is required")
    if session.get(Node, node_id) is None:
        raise _bad_request(f"node_id {node_id} does not exist")


def _check_policy_exists(session: Session, policy_id: int | None) -> None:
    if policy_id is not None and session.get(BackupPolicy, policy_id) is None:
        raise _bad_request(f"backup_policy_id {policy_id} does not exist")


def _check_depends_on(session: Session, slug: str, depends_on: list[str]) -> list[str]:
    """Same rules ``registry/loader.py`` applies at YAML load time: no self
    reference, no unknown slug, no cycle across the whole registry."""
    cleaned: list[str] = []
    for dep in depends_on:
        dep = dep.strip()
        if dep and dep not in cleaned:
            cleaned.append(dep)

    graph = {s.slug: list(s.depends_on or []) for s in session.exec(select(Service)).all()}
    for dep in cleaned:
        if dep == slug:
            raise _bad_request(f"service {slug!r} cannot depend on itself")
        if dep not in graph:
            raise _bad_request(f"depends_on references unknown service {dep!r}")

    graph[slug] = cleaned
    cycle = _find_dependency_cycle(graph)
    if cycle:
        raise _bad_request("depends_on would create a cycle: " + " -> ".join(cycle))
    return cleaned


def _check_backup_requires(
    session: Session,
    slug: str,
    backup_requires: list[str],
    *,
    policy_id: int | None,
) -> list[str]:
    """Same rules the YAML loader applies to ``backup_requires``: known slugs,
    no self reference, no cycle (through other services' requirements or the
    policy's ``requires``). Duplicates collapse. Unrelated to ``depends_on``."""
    cleaned = clean_slug_list(backup_requires)
    if slug in cleaned:
        raise _bad_request(f"service {slug!r} cannot list itself in backup_requires")
    known = set(session.exec(select(Service.slug)).all())
    unknown = [c for c in cleaned if c not in known]
    if unknown:
        raise _bad_request(f"backup_requires references unknown service(s): {', '.join(unknown)}")
    _check_requires_graph(
        session,
        service_requires_override={slug: cleaned},
        policy_id=policy_id,
        slug=slug,
    )
    return cleaned


def _check_requires_graph(
    session: Session,
    *,
    slug: str,
    service_requires_override: dict[str, list[str]] | None = None,
    policy_id: int | None,
) -> None:
    """Refuse a change that would make the requires graph cyclic."""
    policy = session.get(BackupPolicy, policy_id) if policy_id is not None else None
    problems = requires_problems_from_db(
        session,
        service_requires_override=service_requires_override,
        service_policy_override={slug: policy.name if policy else None},
    )
    cycles = [p for p in problems if "cycle" in p]
    if cycles:
        raise _bad_request(cycles[0] + " — backup requirements must not loop back on themselves")


def _check_backup_exclusion(excluded: bool, reason: str) -> None:
    # Mirrors the CLI, which insists on a reason: the reason is what the
    # backup pipeline prints when it refuses, so a blank one helps nobody.
    if excluded and not reason.strip():
        raise _bad_request("backup_excluded=true needs a backup_excluded_reason")


def _check_not_excluded_in_policy(excluded: bool, reason: str, policy_id: int | None) -> None:
    """A service that is never backed up (the VM hosting the PBS datastore, TrueNAS) cannot
    be in a backup policy: the policy would run it every time and the pipeline would only
    refuse it."""
    if excluded and policy_id is not None:
        raise _bad_request(
            f"this service is never backed up ({reason.strip() or 'backup_excluded'}), so it "
            "cannot be in a backup policy - remove the policy, or un-exclude the service"
        )


def _check_ip_address(raw: str | None) -> str:
    """The manual IP through the one shared validator (registry/ip_address.py)."""
    try:
        return validate_ip_address(raw)
    except ValueError as exc:
        raise _bad_request(f"ip_address: {exc}") from None


def _check_transport(kind: ProbeKind, transport: Any, allowed: tuple[str, ...]) -> None:
    if not isinstance(transport, dict):
        raise _bad_request(
            f"{kind.value} probe runs a command and needs a 'transport' object in its config"
        )
    ttype = transport.get("type", "local")
    if ttype not in allowed:
        raise _bad_request(
            f"transport.type {ttype!r} is not valid for {kind.value} "
            f"(expected one of: {', '.join(allowed)})"
        )
    if ttype != "local" and not transport.get("host"):
        raise _bad_request(f"transport.host is required for a {ttype} transport")


def _validate_probe_config(kind: ProbeKind, config: dict[str, Any]) -> None:
    """Catch configs the probe would only reject at run time.

    Driven by the same ``PROBE_SPECS`` table the CLI builder uses: every field
    it marks required must be present, and kinds that run a command need a
    usable ``transport`` block. HTTP/TCP additionally go through the exact
    Pydantic model ``health/probes/network.py`` validates with.
    """
    spec = PROBE_SPECS[kind]
    if kind is ProbeKind.PORTAINER and bool(config.get("stack")) == bool(config.get("container")):
        raise _bad_request("a portainer probe needs exactly one of 'stack' or 'container'")
    missing = [f.key for f in spec.fields if f.required and config.get(f.key) in (None, "", [], {})]
    if missing:
        raise _bad_request(f"{kind.value} probe config is missing: {', '.join(missing)}")

    model = _STRICT_CONFIG_MODELS.get(kind)
    if model is not None:
        try:
            model.model_validate({**config, "kind": kind})
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
            )
            raise _bad_request(f"invalid {kind.value} probe config — {problems}") from None

    if spec.needs_transport:
        _check_transport(kind, config.get("transport"), spec.transport_choices or _TRANSPORT_TYPES)
    elif kind is ProbeKind.PING and config.get("transport") is not None:
        # Optional for ping (default: from the container), checked when given.
        _check_transport(kind, config.get("transport"), _TRANSPORT_TYPES)


def _check_probe_name_free(
    session: Session, service_id: int, name: str, exclude_id: int | None = None
) -> None:
    stmt = select(Probe).where(Probe.service_id == service_id, Probe.name == name)
    clash = session.exec(stmt).first()
    if clash is not None and clash.id != exclude_id:
        raise _conflict(f"this service already has a probe named {name!r}")


# ---------------------------------------------------------------------------
# Registry status / save / reset
# ---------------------------------------------------------------------------


@router.get("/registry/status")
async def registry_status(session: SessionDep) -> dict[str, object]:
    """Whether the DB has unsaved changes vs the on-disk YAML."""
    diff = compute_registry_diff(session)
    return {
        "dirty": diff.dirty,
        "yaml_path": diff.yaml_path,
        "yaml_hash": diff.yaml_hash,
        "db_hash": diff.db_hash,
    }


@router.post("/registry/save")
async def save_registry(
    session: SessionDep,
    settings: SettingsDep,
    adapters: AdaptersDep,
    admin: CurrentAdmin,
) -> dict[str, object]:
    """Save button - write the live DB registry back to YAML, THEN push the
    pending changes to every outside service (Traefik Manager today).

    The YAML is written and committed first and exactly as before; a push
    that fails (or blows up) never undoes it and is reported in
    ``push_results`` / ``push`` instead. Existing callers keep working: the
    original keys are unchanged, the push keys are additive."""
    _ = get_settings()  # ensure singleton warmed
    try:
        sha = save_registry_to_yaml(session, settings.services_yaml_path, actor="dashboard")
        session.commit()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not write YAML: {exc}") from exc
    results = await apply_all(session, adapters, actor=admin.username)
    session.commit()
    return {
        "status": "saved",
        "sha256": sha,
        "path": str(settings.services_yaml_path),
        "push_results": [r.as_dict() for r in results],
        "push": summarize(results),
    }


class PushIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Which outside services (default: all registered, today ``["proxy"]``).
    targets: list[str] | None = None
    #: Restrict to these hostnames.
    hostnames: list[str] | None = None
    #: Only report what would be done.
    dry_run: bool = False
    #: Opt-in: also move these hosts' routes into the config file they resolve to
    #: (``TRAEFIK_MANAGER_CONFIG_FILE`` / their own ``config_file``), after the
    #: normal push. Existing routes are never moved otherwise.
    move_config_files: list[str] | None = None


@router.get("/push/status")
async def push_status(
    session: SessionDep, adapters: AdaptersDep, settings: SettingsDep
) -> dict[str, object]:
    """What is not pushed yet, per outside service. Read-only. Also re-reads the
    proxy catalog (entry points, certificate resolvers, config files), so a
    resolver or file added in Traefik Manager shows up in the next form."""
    await get_catalog(settings, adapters.proxy, refresh=True)
    statuses = await status_all(session, adapters)
    return {"targets": [st.as_dict() for st in statuses]}


@router.post("/push")
async def push_now(
    body: PushIn,
    session: SessionDep,
    adapters: AdaptersDep,
    admin: CurrentAdmin,
    settings: SettingsDep,
) -> dict[str, object]:
    """Push now: bring the outside services in line with the DB, without
    touching services.yaml. Returns one item per thing done (or, with
    ``dry_run``, per thing that would be done); a failing item never stops
    the others."""
    known = {p.name for p in get_pushers()}
    unknown = sorted(set(body.targets or []) - known)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown push target(s): {', '.join(unknown)} (known: {', '.join(sorted(known))})",
        )
    results = await apply_all(
        session,
        adapters,
        names=body.targets,
        only=body.hostnames,
        dry_run=body.dry_run,
        actor=admin.username,
    )
    session.commit()
    if body.move_config_files:
        await get_catalog(settings, adapters.proxy, refresh=True)
        results.extend(await _move_hosts(session, adapters, settings, admin, body))
    return {
        "dry_run": body.dry_run,
        "results": [r.as_dict() for r in results],
        "summary": summarize(results),
    }


async def _move_hosts(
    session: Session, adapters: Any, settings: Any, admin: CurrentAdmin, body: PushIn
) -> list[PushResult]:
    """The opt-in "Move to configured file" part of a push, one result per host."""
    out: list[PushResult] = []
    for hostname in body.move_config_files or []:
        host = hostname.strip().lower()
        ph = session.exec(select(ProxyHost).where(ProxyHost.hostname == host)).first()
        if ph is None:
            out.append(PushResult("proxy", host, "move", False, "no such proxy host"))
        elif body.dry_run:
            out.append(
                PushResult("proxy", host, "move", True, "would move to its configured file", True)
            )
        else:
            out.append(
                await move_to_configured_file(session, adapters, settings, ph, actor=admin.username)
            )
            session.commit()
    return out


@router.post("/registry/reset")
async def reset_registry(session: SessionDep, settings: SettingsDep) -> dict[str, object]:
    """Reset button — re-run boot reconcile from YAML, discarding live DB edits."""
    if not settings.services_yaml_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"YAML not found at {settings.services_yaml_path}",
        )
    spec = reload_registry_from_yaml(session, settings.services_yaml_path)
    session.commit()
    return {
        "status": "reloaded",
        "path": str(settings.services_yaml_path),
        "services": len(spec.services),
        "nodes": len(spec.nodes),
        "policies": len(spec.backup_policies),
    }


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


@router.get("/registry/services")
async def list_services(session: SessionDep) -> list[dict[str, object]]:
    services = session.exec(select(Service).order_by(Service.slug)).all()
    nodes = {n.id: n.name for n in session.exec(select(Node)).all()}
    states = {st.service_id: st for st in session.exec(select(ServiceState)).all()}
    index = TreeIndex(list(services))
    out: list[dict[str, object]] = []
    for s in services:
        ip, source = effective_ip(s, states.get(s.id))
        parent = index.parent(s)
        out.append(
            {
                "slug": s.slug,
                "name": s.name,
                "enabled": s.enabled,
                "parent": parent.slug if parent else None,
                "depth": index.depth(s),
                "group": s.group,
                "node": nodes.get(s.node_id),
                "guest_kind": s.guest_kind.value,
                "guest_id": s.guest_id,
                "ip_address": s.ip_address or "",
                "effective_ip": ip,
                "ip_source": source,
            }
        )
    return out


@router.get("/registry/services/{slug}")
async def get_service(slug: str, session: SessionDep) -> dict[str, Any]:
    return service_detail(session, _get_service(session, slug))


@router.get("/registry/services/{slug}/health-history")
async def get_service_health_history(
    slug: str, session: SessionDep, limit: int = 20
) -> list[dict[str, Any]]:
    """The last ``limit`` periodic :class:`HealthScanSample` rows for this
    service, OLDEST first (so the compact tick-strip on the service page
    reads left-to-right as "earlier -> now", the natural reading direction
    for a trend). Written by :class:`orchestrator.scheduler.health_scanner
    .HealthScanner` — this is deliberately just enough for that strip, not a
    new charting subsystem; the full per-probe detail for the CURRENT verdict
    is already on ``ServiceState`` (see ``get_service``/``service_detail``).
    """
    svc = _get_service(session, slug)
    limit = max(1, min(limit, 200))
    rows = session.exec(
        select(HealthScanSample)
        .where(HealthScanSample.service_id == svc.id)
        .order_by(col(HealthScanSample.sampled_at).desc())
        .limit(limit)
    ).all()
    rows = list(reversed(rows))  # newest-fetched, oldest-returned
    return [
        {
            "sampled_at": r.sampled_at.isoformat(),
            "state": r.state.value,
            "reason": r.reason,
        }
        for r in rows
    ]


@router.post("/registry/services", status_code=status.HTTP_201_CREATED)
async def create_service(
    body: ServiceCreate, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    slug = body.slug.strip()
    if not _SLUG_RE.match(slug):
        raise _bad_request(
            f"invalid slug {slug!r}: use lowercase letters, digits, '-' or '_' "
            "(starting with a letter or digit, max 63 chars)"
        )
    if session.exec(select(Service).where(Service.slug == slug)).first() is not None:
        raise _conflict(f"service {slug!r} already exists")
    name = _require_text(body.name, "name")
    guest_kind = _parse_guest_kind(body.guest_kind)
    index = TreeIndex.build(session)
    parent = _resolve_parent(session, index, body.parent, child=None)
    node_id = body.node_id
    if parent is not None:
        # A sub-service lives where its root lives and has no guest of its own.
        if guest_kind is not GuestKind.NONE or body.guest_id is not None:
            raise _bad_request(
                "a sub-service uses its root's guest - leave guest_kind/guest_id out"
            )
        if body.backup_policy_id is not None:
            raise _bad_request("backups are per guest - set the backup policy on the root service")
        node_id = index.root(parent).node_id
    _check_node_exists(session, node_id)
    _check_policy_exists(session, body.backup_policy_id)
    _check_backup_exclusion(body.backup_excluded, body.backup_excluded_reason)
    _check_not_excluded_in_policy(
        body.backup_excluded, body.backup_excluded_reason, body.backup_policy_id
    )
    depends_on = _check_depends_on(session, slug, body.depends_on)
    backup_requires = _check_backup_requires(
        session,
        slug,
        body.backup_requires,
        policy_id=body.backup_policy_id,
    )
    ip_address = _check_ip_address(body.ip_address)

    svc = Service(
        slug=slug,
        name=name,
        description=body.description,
        group=body.group,
        node_id=node_id,
        parent_id=parent.id if parent is not None else None,
        controller=_check_controller(body.controller),
        recovery=_check_recovery(body.recovery),
        deploy=_check_deploy(body.deploy, _check_controller(body.controller)),
        backup_gate=_parse_backup_gate(body.backup_gate),
        guest_kind=guest_kind,
        guest_id=body.guest_id,
        enabled=body.enabled,
        backup_excluded=body.backup_excluded,
        backup_excluded_reason=body.backup_excluded_reason,
        backup_policy_id=body.backup_policy_id,
        depends_on=depends_on,
        backup_requires=backup_requires,
        ip_address=ip_address,
    )
    session.add(svc)
    session.flush()
    audit.record(
        session,
        actor=admin.username,
        action="service.created",
        target=f"service:{slug}",
        details=body.model_dump(),
    )
    session.commit()
    session.refresh(svc)
    return service_detail(session, svc)


@router.put("/registry/services/{slug}")
async def update_service(
    slug: str, body: ServiceUpdate, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    return apply_service_update(session, admin.username, slug, body)


def apply_service_update(
    session: Session, actor: str, slug: str, body: ServiceUpdate
) -> dict[str, Any]:
    """``PUT /api/registry/services/{slug}``, callable: every validation,
    the write and the ``service.updated`` audit entry. The bulk endpoint
    (``web/routers/bulk.py``) runs exactly this per service."""
    svc = _get_service(session, slug)
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise _bad_request("nothing to change — send at least one field")

    if "name" in changes:
        changes["name"] = _require_text(changes["name"] or "", "name")
    if "description" in changes and changes["description"] is None:
        changes["description"] = ""
    if "group" in changes and changes["group"] is None:
        changes["group"] = ""
    if "backup_excluded_reason" in changes and changes["backup_excluded_reason"] is None:
        changes["backup_excluded_reason"] = ""
    for flag in ("enabled", "backup_excluded", "wake_on_hit_enabled"):
        if flag in changes and changes[flag] is None:
            raise _bad_request(f"{flag} must be true or false")
    if "guest_kind" in changes:
        changes["guest_kind"] = _parse_guest_kind(changes["guest_kind"] or "")
    if "controller" in changes:
        changes["controller"] = _check_controller(changes["controller"])
    if "recovery" in changes:
        changes["recovery"] = _check_recovery(changes["recovery"])
    if "deploy" in changes or "controller" in changes:
        # A deploy needs a Portainer stack link, so check the pair as it will be saved.
        changes["deploy"] = _check_deploy(
            changes.get("deploy", svc.deploy),
            changes.get("controller", svc.controller or {}),
        )
    if "backup_gate" in changes:
        changes["backup_gate"] = _parse_backup_gate(changes["backup_gate"])
    index = TreeIndex.build(session)
    new_parent = index.parent(svc)
    if "parent" in changes:
        new_parent = _resolve_parent(session, index, changes.pop("parent"), child=svc)
        changes["parent_id"] = new_parent.id if new_parent is not None else None
    if new_parent is not None:
        # Sub-service: guest, node and backup policy come from the root.
        for field in ("guest_kind", "guest_id", "node_id", "backup_policy_id"):
            if field in changes and changes[field] not in (None, GuestKind.NONE.value, "none"):
                raise _bad_request(
                    f"{field} is set on the root service ({index.root(new_parent).slug}); "
                    "a sub-service inherits it"
                )
            changes.pop(field, None)
        changes["guest_kind"] = GuestKind.NONE
        changes["guest_id"] = None
        changes["backup_policy_id"] = None
        changes["node_id"] = index.root(new_parent).node_id
    if "wake_trigger" in changes:
        try:
            changes["wake_trigger"] = WakeTrigger(str(changes["wake_trigger"] or "").strip())
        except ValueError:
            raise _bad_request("wake_trigger must be 'call' or 'button'") from None
    if "ip_address" in changes:
        changes["ip_address"] = _check_ip_address(changes["ip_address"])
    if "node_id" in changes:
        _check_node_exists(session, changes["node_id"])
    if "backup_policy_id" in changes:
        _check_policy_exists(session, changes["backup_policy_id"])
    if "depends_on" in changes:
        changes["depends_on"] = _check_depends_on(session, slug, changes["depends_on"] or [])
    if "backup_requires" in changes:
        changes["backup_requires"] = _check_backup_requires(
            session,
            slug,
            changes["backup_requires"] or [],
            policy_id=changes.get("backup_policy_id", svc.backup_policy_id),
        )
    elif "backup_policy_id" in changes:
        # Re-pointing a service at another policy changes which `requires` it
        # inherits, which can close a loop even though its own list did not move.
        _check_requires_graph(
            session,
            slug=slug,
            policy_id=changes.get("backup_policy_id", svc.backup_policy_id),
        )
    _check_backup_exclusion(
        changes.get("backup_excluded", svc.backup_excluded),
        changes.get("backup_excluded_reason", svc.backup_excluded_reason),
    )
    if changes.get("backup_excluded") and svc.backup_policy_id is not None:
        # Excluding a service that is in a policy takes it out of it (unless this very
        # request tries to keep it there, which the check below refuses).
        changes.setdefault("backup_policy_id", None)
    _check_not_excluded_in_policy(
        changes.get("backup_excluded", svc.backup_excluded),
        changes.get("backup_excluded_reason", svc.backup_excluded_reason),
        changes.get("backup_policy_id", svc.backup_policy_id),
    )

    state = session.get(ServiceState, svc.id) if "ip_address" in changes else None
    ip_before = effective_ip(svc, state)
    for key, value in changes.items():
        setattr(svc, key, value)
    svc.updated_at = _utcnow()
    session.add(svc)
    session.flush()
    # Everything under this service lives where its root lives; a switched-off
    # service (or one moved under another parent) changes which hosts are served.
    tree = TreeIndex.build(session)
    for child in tree.descendants(svc):
        root_node = tree.root(child).node_id
        if child.node_id != root_node:
            child.node_id = root_node
            session.add(child)
    if {"enabled", "parent_id"} & set(changes):
        from orchestrator.config import get_settings
        from orchestrator.registry.host_cascade import resync_subtree_probes

        resync_subtree_probes(session, get_settings(), svc)
    audit.record(
        session,
        actor=actor,
        action="service.updated",
        target=f"service:{slug}",
        details=body.model_dump(exclude_unset=True),
    )
    ip_after = effective_ip(svc, state)
    if "ip_address" in changes and ip_after[0] != ip_before[0]:
        audit.record(
            session,
            actor=actor,
            action="service.ip_changed",
            target=f"service:{slug}",
            details={"old": ip_before[0], "new": ip_after[0], "source": ip_after[1]},
        )
    session.commit()
    session.refresh(svc)
    return service_detail(session, svc)


@router.delete("/registry/services/{slug}")
async def delete_service(
    slug: str, session: SessionDep, admin: CurrentAdmin, cascade: bool = False
) -> dict[str, Any]:
    """Remove a service. Its probes and runtime state go with it (FK CASCADE);
    backup records and pipeline runs survive with ``service_id`` nulled
    (SET NULL) — the same history-preserving policy as removing it from YAML.
    A service with sub-services is removed together with them, but only with
    ``?cascade=true``."""
    svc = _get_service(session, slug)
    tree = TreeIndex.build(session)
    below = tree.descendants(svc)
    if below and not cascade:
        raise _conflict(
            f"{slug!r} has sub-services ({', '.join(c.slug for c in below)}) - "
            "delete them too with ?cascade=true, or move them first"
        )
    doomed = {slug, *(c.slug for c in below)}
    dependents = sorted(
        s.slug
        for s in session.exec(select(Service)).all()
        if s.slug not in doomed and doomed & set(s.depends_on or [])
    )
    if dependents:
        raise _conflict(
            f"cannot delete {slug!r}: {', '.join(dependents)} depend(s) on it — "
            "remove it from their depends_on first"
        )
    required_by = sorted(
        {
            s.slug
            for s in session.exec(select(Service)).all()
            if s.slug not in doomed and doomed & set(s.backup_requires or [])
        }
        | {
            f"policy {p.name}"
            for p in session.exec(select(BackupPolicy)).all()
            if doomed & set(p.requires or [])
        }
    )
    if required_by:
        raise _conflict(
            f"cannot delete {slug!r}: {', '.join(required_by)} require(s) it for backups — "
            "remove it from their backup requirements first"
        )
    probe_names = [p.name for p in _service_probes(session, svc)]
    for child in reversed(below):  # deepest first, so no row ever loses its parent under us
        probe_names += [p.name for p in _service_probes(session, child)]
        session.delete(child)
        session.flush()
    session.delete(svc)
    audit.record(
        session,
        actor=admin.username,
        action="service.deleted",
        target=f"service:{slug}",
        details={
            "name": svc.name,
            "probes_removed": probe_names,
            "sub_services_removed": [c.slug for c in below],
        },
    )
    session.commit()
    return {"status": "deleted", "slug": slug, "probes_removed": len(probe_names)}


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


@router.get("/registry/nodes")
async def list_nodes(session: SessionDep) -> list[dict[str, Any]]:
    nodes = session.exec(select(Node).order_by(Node.name)).all()
    # Homelab scale (a handful of services), so a Python-side tally is
    # simpler than a GROUP BY and plenty fast.
    counts = Counter(session.exec(select(Service.node_id)).all())
    return [{**_node_dict(n), "service_count": counts.get(n.id, 0)} for n in nodes]


@router.post("/registry/nodes", status_code=status.HTTP_201_CREATED)
async def create_node(body: NodeCreate, session: SessionDep, admin: CurrentAdmin) -> dict[str, Any]:
    name = _require_text(body.name, "name")
    if session.exec(select(Node).where(Node.name == name)).first() is not None:
        raise _conflict(f"node {name!r} already exists")
    node = Node(
        name=name,
        always_on=body.always_on,
        power_mgr_target=body.power_mgr_target,
        notes=body.notes,
        address=_check_node_address(body.address),
        wake_on_hit_enabled=body.wake_on_hit_enabled,
    )
    session.add(node)
    session.flush()
    audit.record(
        session,
        actor=admin.username,
        action="node.created",
        target=f"node:{name}",
        details=body.model_dump(),
    )
    session.commit()
    session.refresh(node)
    return _node_dict(node)


@router.put("/registry/nodes/{name}")
async def update_node(
    name: str, body: NodeUpdate, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    return apply_node_update(session, admin.username, name, body)


def apply_node_update(session: Session, actor: str, name: str, body: NodeUpdate) -> dict[str, Any]:
    """``PUT /api/registry/nodes/{name}``, callable (reused by the bulk endpoint)."""
    node = _get_node(session, name)
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise _bad_request("nothing to change — send at least one field")
    if "name" in changes:
        new_name = _require_text(changes["name"] or "", "name")
        clash = session.exec(select(Node).where(Node.name == new_name)).first()
        if clash is not None and clash.id != node.id:
            raise _conflict(f"node {new_name!r} already exists")
        changes["name"] = new_name
    for flag in ("always_on", "wake_on_hit_enabled"):
        if flag in changes and changes[flag] is None:
            raise _bad_request(f"{flag} must be true or false")
    for text_field in ("power_mgr_target", "notes", "address"):
        if text_field in changes and changes[text_field] is None:
            changes[text_field] = ""
    if "address" in changes:
        changes["address"] = _check_node_address(changes["address"])

    for key, value in changes.items():
        setattr(node, key, value)
    node.updated_at = _utcnow()
    session.add(node)
    audit.record(
        session,
        actor=actor,
        action="node.updated",
        target=f"node:{name}",
        details=body.model_dump(exclude_unset=True),
    )
    session.commit()
    session.refresh(node)
    return _node_dict(node)


@router.delete("/registry/nodes/{name}")
async def delete_node(name: str, session: SessionDep, admin: CurrentAdmin) -> dict[str, Any]:
    """Refused while any service still lives on the node — ``service.node_id``
    is ON DELETE RESTRICT by design (see docs/architecture.md)."""
    node = _get_node(session, name)
    users = session.exec(select(Service.slug).where(Service.node_id == node.id)).all()
    if users:
        raise _conflict(
            f"cannot delete node {name!r}: still used by {', '.join(sorted(users))} — "
            "move or delete those services first"
        )
    session.delete(node)
    audit.record(session, actor=admin.username, action="node.deleted", target=f"node:{name}")
    session.commit()
    return {"status": "deleted", "name": name}


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


@router.get("/registry/services/{slug}/probes")
async def list_probes(slug: str, session: SessionDep) -> list[dict[str, Any]]:
    svc = _get_service(session, slug)
    return [_probe_dict(p) for p in _service_probes(session, svc)]


@router.post("/registry/probes/{probe_id}/test")
async def test_probe(probe_id: int, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    """Run this one probe right now and return its raw result.

    Bypasses the service's aggregate verdict and persists nothing to
    ServiceState — same primitive ``scan``/the CLI's ``probe-test`` use
    (``HealthEngine.run_probe``), exposed here so a probe reporting
    UNKNOWN or FAILED can be debugged from the dashboard: the exact
    request made, the status code, the raw connection error.
    """
    probe, _svc = _get_probe(session, probe_id)
    engine = HealthEngine(settings=settings)
    result = await engine.run_probe(probe)
    return result.model_dump()


@router.post("/registry/services/{slug}/probes", status_code=status.HTTP_201_CREATED)
async def create_probe(
    slug: str, body: ProbeCreate, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    svc = _get_service(session, slug)
    assert svc.id is not None
    kind = _parse_probe_kind(body.kind)
    name = _require_text(body.name, "name")
    _check_probe_name_free(session, svc.id, name)
    _validate_probe_config(kind, body.config)
    timeout_s = (
        body.timeout_s if body.timeout_s is not None else PROBE_SPECS[kind].default_timeout_s
    )
    if timeout_s <= 0:
        raise _bad_request("timeout_s must be positive")
    order = body.order
    if order is None:
        existing = _service_probes(session, svc)
        order = max((p.order for p in existing), default=-1) + 1

    probe = Probe(
        service_id=svc.id,
        name=name,
        kind=kind,
        required=body.required,
        timeout_s=timeout_s,
        order=order,
        config=body.config,
        enabled=body.enabled,
    )
    session.add(probe)
    session.flush()
    audit.record(
        session,
        actor=admin.username,
        action="probe.created",
        target=f"service:{slug}",
        details={"probe_id": probe.id, **body.model_dump()},
    )
    session.commit()
    session.refresh(probe)
    return _probe_dict(probe)


@router.put("/registry/probes/{probe_id}")
async def update_probe(
    probe_id: int, body: ProbeUpdate, session: SessionDep, admin: CurrentAdmin
) -> dict[str, Any]:
    return apply_probe_update(session, admin.username, probe_id, body)


def apply_probe_update(
    session: Session, actor: str, probe_id: int, body: ProbeUpdate
) -> dict[str, Any]:
    """``PUT /api/registry/probes/{probe_id}``, callable (reused by the bulk
    "set all probes required/optional" action, one call per probe)."""
    probe, svc = _get_probe(session, probe_id)
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise _bad_request("nothing to change — send at least one field")

    if "name" in changes:
        changes["name"] = _require_text(changes["name"] or "", "name")
        _check_probe_name_free(session, probe.service_id, changes["name"], exclude_id=probe.id)
    if "kind" in changes:
        changes["kind"] = _parse_probe_kind(changes["kind"] or "")
    for key in ("required", "timeout_s", "order", "enabled"):
        if key in changes and changes[key] is None:
            raise _bad_request(f"{key} must not be null")
    if "timeout_s" in changes and changes["timeout_s"] <= 0:
        raise _bad_request("timeout_s must be positive")
    if "config" in changes and changes["config"] is None:
        changes["config"] = {}
    # Re-validate whenever either half of (kind, config) changes: switching
    # kind with the old config left in place is just as broken as a bad config.
    if "kind" in changes or "config" in changes:
        _validate_probe_config(
            changes.get("kind", probe.kind), changes.get("config", probe.config) or {}
        )

    for key, value in changes.items():
        setattr(probe, key, value)
    session.add(probe)
    audit.record(
        session,
        actor=actor,
        action="probe.updated",
        target=f"service:{svc.slug}",
        details={"probe_id": probe_id, **body.model_dump(exclude_unset=True)},
    )
    session.commit()
    session.refresh(probe)
    return _probe_dict(probe)


def set_service_probes_required(
    session: Session, actor: str, slug: str, required: bool
) -> dict[str, Any]:
    """Mark every probe of ``slug`` required (gates backups) or optional, via
    :func:`apply_probe_update` for each probe that changes - same validation
    and the same ``probe.updated`` audit entry as editing them one by one."""
    svc = _get_service(session, slug)
    probes = _service_probes(session, svc)
    changed = 0
    for probe in probes:
        if probe.required != required:
            assert probe.id is not None
            apply_probe_update(session, actor, probe.id, ProbeUpdate(required=required))
            changed += 1
    word = "required" if required else "optional"
    if not probes:
        message = "no probes"
    elif changed:
        message = f"{changed} of {len(probes)} probe(s) now {word}"
    else:
        message = f"all {len(probes)} probe(s) already {word}"
    return {"slug": slug, "probes": len(probes), "changed": changed, "message": message}


@router.delete("/registry/probes/{probe_id}")
async def delete_probe(probe_id: int, session: SessionDep, admin: CurrentAdmin) -> dict[str, Any]:
    probe, svc = _get_probe(session, probe_id)
    details = {"probe_id": probe_id, "name": probe.name, "kind": probe.kind.value}
    session.delete(probe)
    audit.record(
        session,
        actor=admin.username,
        action="probe.deleted",
        target=f"service:{svc.slug}",
        details=details,
    )
    session.commit()
    return {"status": "deleted", "id": probe_id}


# ---------------------------------------------------------------------------
# Manual guest power (raw start/stop, NOT the /wake pipeline)
# ---------------------------------------------------------------------------


class GuestPowerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["start", "stop"]


def _backup_currently_running(session: Session, service_id: int) -> bool:
    """Is there a backup ``PipelineRun`` for this service that is currently
    ``RUNNING``? No existing helper does this query elsewhere (the backup
    pipeline runs synchronously within one request — see
    ``pipelines/backup.py`` — so this normally only matches a *concurrent*
    in-flight request), so this is the guardrail
    :func:`service_guest_power` uses to refuse stopping a guest mid-backup."""
    return (
        session.exec(
            select(PipelineRun.id)
            .where(
                PipelineRun.service_id == service_id,
                PipelineRun.kind == PipelineKind.BACKUP,
                PipelineRun.status == PipelineStatus.RUNNING,
            )
            .limit(1)
        ).first()
        is not None
    )


@router.post("/services/{slug}/guest/power")
async def service_guest_power(
    slug: str,
    body: GuestPowerIn,
    session: SessionDep,
    adapters: AdaptersDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    """Raw, direct Proxmox guest start/stop for one service.

    Deliberately NOT the same thing as ``POST /wake/{slug}`` (the full
    orchestrated pipeline: node power, hold, guest start, health wait) — this
    calls ``adapters.proxmox.start_guest``/``stop_guest`` straight through,
    for when an admin wants exactly that and nothing else. The
    node/vmid/kind resolution mirrors
    :meth:`orchestrator.pipelines.wake.WakePipeline._ensure_guest_running`.

    Guardrail: ``stop`` is refused (``409``) while a backup pipeline run is
    currently ``RUNNING`` for this service — see
    :func:`_backup_currently_running` — to avoid yanking the guest out from
    under a live ``vzdump``. ``start`` has no such guard; there is no
    equivalent hazard for starting a guest back up.
    """
    return await guest_power_for_service(session, adapters, admin.username, slug, body.action)


async def guest_power_for_service(
    session: Session, adapters: Any, actor: str, slug: str, action: str
) -> dict[str, Any]:
    """:func:`service_guest_power`, callable (the guests page's bulk Start/Stop
    and ``POST /api/infra/guests/{node}/{vmid}/power`` reuse it so the
    mid-backup guardrail always applies)."""
    svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
    if svc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no service registered with slug {slug!r}")
    if svc.guest_kind is GuestKind.NONE or svc.guest_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"service {slug!r} has no guest configured"
        )
    node = session.get(Node, svc.node_id) if svc.node_id is not None else None
    if node is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"service {slug!r} has no node")
    assert svc.id is not None

    if action == "stop" and _backup_currently_running(session, svc.id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"a backup is currently running for {slug!r} — refusing to stop its "
            "guest mid-backup",
        )

    try:
        if action == "start":
            handle = await adapters.proxmox.start_guest(node.name, svc.guest_id, svc.guest_kind)
        else:
            handle = await adapters.proxmox.stop_guest(node.name, svc.guest_id, svc.guest_kind)
    except AdapterError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    audit.record(
        session,
        actor=actor,
        action="service.guest_power_action",
        target=f"service:{slug}",
        details={
            "action": action,
            "node": node.name,
            "vmid": svc.guest_id,
            "guest_kind": svc.guest_kind.value,
            "upid": handle.upid,
        },
    )
    session.commit()
    return {"status": "ok", "service": slug, "action": action, "upid": handle.upid}


# ---------------------------------------------------------------------------
# Service IP (see docs/service_ips.md)
# ---------------------------------------------------------------------------


@router.get("/services/{slug}/ip")
async def get_service_ip(slug: str, session: SessionDep) -> dict[str, Any]:
    """Effective IP (manual, else discovered), where it came from, the MACs."""
    svc = _get_service(session, slug)
    return {"slug": slug, **ip_view(svc, session.get(ServiceState, svc.id))}


@router.post("/services/{slug}/ip/discover")
async def discover_service_ip_endpoint(
    slug: str,
    session: SessionDep,
    adapters: AdaptersDep,
    settings: SettingsDep,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    """Run IP discovery for this one service now (guest agent / LXC, then ARP,
    then DHCP). A stopped guest is skipped, not an error. Never touches the
    manual ``ip_address``."""
    return await discover_service_ip(session, adapters, settings, slug, actor=admin.username)


async def discover_service_ip(
    session: Session, adapters: Any, settings: Any, slug: str, *, actor: str
) -> dict[str, Any]:
    """:func:`discover_service_ip_endpoint`, callable (the bulk ``discover_ip``
    action runs exactly this per service)."""
    svc = _get_service(session, slug)
    discoverer = IpDiscoverer(settings=settings, proxmox=adapters.proxmox)
    result = await discoverer.discover(session, svc, actor=actor)
    session.refresh(svc)
    return {**result.as_dict(), **ip_view(svc, session.get(ServiceState, svc.id))}
