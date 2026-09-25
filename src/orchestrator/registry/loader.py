"""YAML ↔ DB reconciler.

Behaviour contract (per the design decision recorded in the phase-1 plan):

* **YAML is the SAVED source of truth.**  On boot, the DB registry tables are
  overwritten from ``services.yaml``.  Runtime tables (state, runs, backups,
  audit, approvals) and admin/passkey tables are never touched.
* **DB is the LIVE working copy.**  Dashboard edits go to the DB.
* :func:`compute_registry_diff` flags whether the DB differs from the last-loaded
  YAML, so the UI can show an unsaved-changes badge.
* :func:`save_registry_to_yaml` serialises the DB back to disk (called by the
  Save button).
* :func:`reload_registry_from_yaml` is the Reset button — re-runs the boot flow.

Removing a service from YAML deletes its ``Service`` row (and cascades
``ServiceState`` + ``Probe`` rows).  ``BackupRecord`` rows are preserved via
``ON DELETE SET NULL`` and the denormalised ``service_slug`` column.
"""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path

from ruamel.yaml import YAML
from sqlmodel import Session, select

from orchestrator.db.models import (
    SETTING_LAST_YAML_LOAD,
    SETTING_LAST_YAML_SAVE,
    BackupPolicy,
    Node,
    Probe,
    ProxyHost,
    Service,
    Setting,
)
from orchestrator.domain.enums import RouterProvider
from orchestrator.logging_setup import get_logger
from orchestrator.registry.schema import (
    BackupPolicySpec,
    NodeSpec,
    ProbeSpec,
    ProxyHostSpec,
    RegistryFile,
    ServiceSpec,
)

log = get_logger(__name__)

_yaml = YAML(typ="rt")  # round-trip: preserves comments + key order
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.preserve_quotes = True


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class RegistryPathError(Exception):
    """The registry path is unusable — missing, a directory, or unreadable.

    Carries an actionable message rather than a bare OSError, because the most
    common cause is a Docker bind mount whose host file did not exist (Docker
    then silently creates a *directory* at the mount point).
    """


def validate_registry_path(path: Path) -> None:
    """Fail fast with a message that says what to do about it."""
    if path.is_dir():
        raise RegistryPathError(
            f"{path} is a DIRECTORY, not a file. This almost always means a Docker "
            f"bind mount pointed at a host file that did not exist, so Docker created "
            f"a directory at the mount point. Fix: stop the stack, remove the empty "
            f"directory on the HOST (rmdir config/services.yaml), create the real file "
            f"(cp config/services.example.yaml config/services.yaml), then start again."
        )
    if not path.exists():
        raise RegistryPathError(
            f"{path} does not exist. Create it from the example: "
            f"cp config/services.example.yaml config/services.yaml"
        )
    if not path.is_file():
        raise RegistryPathError(f"{path} exists but is not a regular file.")


def read_yaml_file(path: Path) -> tuple[RegistryFile, str]:
    """Parse ``services.yaml`` into a validated :class:`RegistryFile` plus its
    content hash. Raises if the file is missing or malformed."""
    validate_registry_path(path)
    raw = path.read_bytes()
    doc = _yaml.load(io.BytesIO(raw)) or {}
    # ruamel returns its own mapping type — coerce via model_validate.
    parsed = RegistryFile.model_validate(dict(doc))
    return parsed, _sha256_bytes(raw)


def write_yaml_file(path: Path, registry: RegistryFile) -> str:
    """Serialise a registry back to YAML on disk (atomic via ``.tmp`` swap).
    Returns the sha256 of the bytes written."""
    # mode="json" so enums serialise to their string values and datetimes/
    # paths are stringified — ruamel's default representer only handles primitives.
    body = registry.model_dump(mode="json", exclude_defaults=False)
    buf = io.BytesIO()
    _yaml.dump(body, buf)
    data = buf.getvalue()

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return _sha256_bytes(data)


# ---------------------------------------------------------------------------
# DB <-> spec conversion
# ---------------------------------------------------------------------------


def _spec_from_db(session: Session) -> RegistryFile:
    """Serialise the DB's current registry state into a :class:`RegistryFile`.
    Order is deterministic (by name/slug) so YAML diffs stay clean."""
    nodes = session.exec(select(Node).order_by(Node.name)).all()
    policies = session.exec(select(BackupPolicy).order_by(BackupPolicy.name)).all()
    services = session.exec(select(Service).order_by(Service.slug)).all()

    node_by_id = {n.id: n for n in nodes}
    policy_by_id = {p.id: p for p in policies}

    services_out: list[ServiceSpec] = []
    for svc in services:
        probes = session.exec(
            select(Probe).where(Probe.service_id == svc.id).order_by(Probe.order, Probe.name)
        ).all()
        proxy_hosts = session.exec(
            select(ProxyHost).where(ProxyHost.service_id == svc.id).order_by(ProxyHost.hostname)
        ).all()

        node_name = node_by_id[svc.node_id].name if svc.node_id in node_by_id else ""
        policy_name = (
            policy_by_id[svc.backup_policy_id].name
            if svc.backup_policy_id in policy_by_id
            else None
        )

        services_out.append(
            ServiceSpec(
                slug=svc.slug,
                name=svc.name,
                description=svc.description,
                node=node_name,
                guest_kind=svc.guest_kind,
                guest_id=svc.guest_id,
                enabled=svc.enabled,
                backup_excluded=svc.backup_excluded,
                backup_excluded_reason=svc.backup_excluded_reason,
                depends_on=list(svc.depends_on or []),
                backup_policy=policy_name,
                probes=[
                    ProbeSpec(
                        name=p.name,
                        kind=p.kind,
                        required=p.required,
                        timeout_s=p.timeout_s,
                        order=p.order,
                        config=p.config,
                    )
                    for p in probes
                ],
                proxy_hosts=[
                    ProxyHostSpec(
                        hostname=ph.hostname,
                        upstream=ph.upstream,
                        router_provider=ph.router_provider.value,
                        extra=ph.extra,
                    )
                    for ph in proxy_hosts
                ],
            )
        )

    return RegistryFile(
        version=1,
        nodes=[
            NodeSpec(
                name=n.name,
                always_on=n.always_on,
                power_mgr_target=n.power_mgr_target,
                notes=n.notes,
            )
            for n in nodes
        ],
        backup_policies=[
            BackupPolicySpec(
                name=p.name,
                schedule_cron=p.schedule_cron,
                mode=p.mode,
                retention=p.retention,
                targets=p.targets,
            )
            for p in policies
        ],
        services=services_out,
    )


def _hash_spec(spec: RegistryFile) -> str:
    """Deterministic hash of a spec for drift detection.

    Independent of on-disk formatting AND of list ordering: nodes / policies /
    services / probes / proxy_hosts are sorted by their identifying key before
    hashing, so a YAML file that lists services in ad-hoc order still hashes
    equal to the DB spec (which is emitted in sorted order).
    """
    import json

    body = spec.model_dump(mode="json")
    body["nodes"] = sorted(body.get("nodes", []), key=lambda n: n["name"])
    body["backup_policies"] = sorted(body.get("backup_policies", []), key=lambda p: p["name"])
    services = sorted(body.get("services", []), key=lambda s: s["slug"])
    for svc in services:
        svc["probes"] = sorted(
            svc.get("probes", []),
            key=lambda p: (p.get("order", 0), p["name"]),
        )
        svc["proxy_hosts"] = sorted(svc.get("proxy_hosts", []), key=lambda ph: ph["hostname"])
    body["services"] = services
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(canonical)


# ---------------------------------------------------------------------------
# Boot reconcile — YAML wins.
# ---------------------------------------------------------------------------


def _find_dependency_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """Return one dependency cycle as a slug path, or None.

    Cycles must be caught here: the health engine walks dependencies before
    scanning, so a loop would recurse until the stack blew rather than producing
    a usable error.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(graph, WHITE)

    def visit(node: str, path: list[str]) -> list[str] | None:
        colour[node] = GREY
        for dep in graph.get(node, []):
            if dep not in colour:
                continue  # unknown slug — already reported as a missing reference
            if colour[dep] == GREY:
                return [*path, node, dep]  # closes the loop
            if colour[dep] == WHITE:
                found = visit(dep, [*path, node])
                if found:
                    return found
        colour[node] = BLACK
        return None

    for node in graph:
        if colour[node] == WHITE:
            found = visit(node, [])
            if found:
                return found
    return None


def validate_references(parsed: RegistryFile) -> None:
    """Check every cross-reference inside the file resolves within the file."""
    node_names = {n.name for n in parsed.nodes}
    policy_names = {p.name for p in parsed.backup_policies}
    slugs = {s.slug for s in parsed.services}

    problems: list[str] = []
    for svc in parsed.services:
        for dep in svc.depends_on:
            if dep == svc.slug:
                problems.append(f"service {svc.slug!r} lists itself in depends_on")
            elif dep not in slugs:
                known = ", ".join(sorted(slugs)) or "(none)"
                problems.append(
                    f"service {svc.slug!r} depends_on {dep!r}, which this file does "
                    f"not define. Services defined here: {known}"
                )
        if svc.node not in node_names:
            known = ", ".join(sorted(node_names)) or "(none defined)"
            problems.append(
                f"service {svc.slug!r} references node {svc.node!r}, which this "
                f"file does not define. Nodes defined here: {known}"
            )
        if svc.backup_policy and svc.backup_policy not in policy_names:
            known = ", ".join(sorted(policy_names)) or "(none defined)"
            problems.append(
                f"service {svc.slug!r} references backup_policy "
                f"{svc.backup_policy!r}, which this file does not define. "
                f"Policies defined here: {known}"
            )

    cycle = _find_dependency_cycle({s.slug: list(s.depends_on) for s in parsed.services})
    if cycle:
        problems.append("depends_on forms a cycle: " + " -> ".join(cycle))

    if problems:
        joined = "\n  - ".join(problems)
        raise ValueError(f"services.yaml has unresolved references:\n  - {joined}")

    # Contradictory rather than invalid: exclusion wins, but say so, because the
    # operator clearly meant one of the two and should know which lost.
    for svc in parsed.services:
        if svc.backup_excluded and svc.backup_policy:
            log.warning(
                "registry.service.excluded_but_has_policy",
                service=svc.slug,
                backup_policy=svc.backup_policy,
                resolution="backup_excluded wins; that policy will never run for it",
            )


def reconcile_yaml_into_db(session: Session, path: Path) -> RegistryFile:
    """Read ``path`` and overwrite the DB's registry tables to match.

    Runtime tables (state, runs, backups, audit, approvals) and admin/passkey
    tables are NOT touched.  Returns the loaded spec so callers can inspect.
    """
    parsed, sha = read_yaml_file(path)
    log.info("registry.yaml.load", path=str(path), sha256=sha[:12])

    # Validate the whole document BEFORE mutating anything. Two reasons:
    #   * a dangling reference is an authoring error and deserves a message that
    #     names the service and the missing node, not a downstream
    #     "FOREIGN KEY constraint failed";
    #   * checking references against the FILE (not the DB) keeps the error
    #     independent of the order in which rows happen to be deleted.
    validate_references(parsed)

    # ORDERING RULE: upsert parents first, then reconcile children, and only
    # then delete orphaned parents. `service.node_id` is ON DELETE RESTRICT, so
    # deleting a node while any service still points at it raises
    # "FOREIGN KEY constraint failed". That happens whenever the node set is
    # replaced wholesale — e.g. swapping the example registry for a real one.

    # --- Nodes: upsert only (deletions deferred to the end) ------------------
    yaml_node_names = {n.name for n in parsed.nodes}
    existing_nodes = {n.name: n for n in session.exec(select(Node)).all()}

    for spec in parsed.nodes:
        node = existing_nodes.get(spec.name)
        if node is None:
            node = Node(name=spec.name)
            session.add(node)
        node.always_on = spec.always_on
        node.power_mgr_target = spec.power_mgr_target
        node.notes = spec.notes
        node.updated_at = datetime.now(UTC)

    session.flush()

    # --- BackupPolicies: upsert only (deletions deferred) --------------------
    yaml_policy_names = {p.name for p in parsed.backup_policies}
    existing_policies = {p.name: p for p in session.exec(select(BackupPolicy)).all()}

    for spec in parsed.backup_policies:
        pol = existing_policies.get(spec.name)
        if pol is None:
            pol = BackupPolicy(name=spec.name)
            session.add(pol)
        pol.schedule_cron = spec.schedule_cron
        pol.mode = spec.mode
        pol.retention = spec.retention
        pol.targets = spec.targets
        pol.updated_at = datetime.now(UTC)

    session.flush()

    # Reload maps now that IDs are settled.
    nodes_by_name = {n.name: n for n in session.exec(select(Node)).all()}
    policies_by_name = {p.name: p for p in session.exec(select(BackupPolicy)).all()}

    # --- Services + Probes + ProxyHosts -------------------------------------
    yaml_service_slugs = {s.slug for s in parsed.services}
    existing_services = {s.slug: s for s in session.exec(select(Service)).all()}

    for spec in parsed.services:
        node = nodes_by_name.get(spec.node)
        if node is None:
            raise ValueError(f"service {spec.slug!r} references unknown node {spec.node!r}")
        policy = policies_by_name.get(spec.backup_policy) if spec.backup_policy else None
        if spec.backup_policy and policy is None:
            raise ValueError(
                f"service {spec.slug!r} references unknown backup_policy " f"{spec.backup_policy!r}"
            )

        svc = existing_services.get(spec.slug)
        if svc is None:
            svc = Service(slug=spec.slug, name=spec.name)
            session.add(svc)
        svc.name = spec.name
        svc.description = spec.description
        svc.guest_kind = spec.guest_kind
        svc.guest_id = spec.guest_id
        svc.enabled = spec.enabled
        svc.backup_excluded = spec.backup_excluded
        svc.backup_excluded_reason = spec.backup_excluded_reason
        svc.depends_on = list(spec.depends_on)
        svc.node_id = node.id
        svc.backup_policy_id = policy.id if policy else None
        svc.updated_at = datetime.now(UTC)
        session.flush()  # need svc.id for children

        # Wipe & replace probes / proxy hosts — cheaper than diffing and safe
        # because they carry no runtime state.
        for old_probe in session.exec(select(Probe).where(Probe.service_id == svc.id)).all():
            session.delete(old_probe)
        for old_ph in session.exec(select(ProxyHost).where(ProxyHost.service_id == svc.id)).all():
            session.delete(old_ph)
        session.flush()

        for pspec in spec.probes:
            session.add(
                Probe(
                    service_id=svc.id,  # type: ignore[arg-type]
                    name=pspec.name,
                    kind=pspec.kind,
                    required=pspec.required,
                    timeout_s=pspec.timeout_s,
                    order=pspec.order,
                    config=pspec.config,
                )
            )
        for phspec in spec.proxy_hosts:
            session.add(
                ProxyHost(
                    service_id=svc.id,
                    hostname=phspec.hostname,
                    upstream=phspec.upstream,
                    router_provider=RouterProvider(phspec.router_provider),
                    extra=phspec.extra,
                )
            )

    for slug, svc in existing_services.items():
        if slug not in yaml_service_slugs:
            session.delete(svc)

    session.flush()

    # --- Deferred parent deletions -------------------------------------------
    # Safe only now: every service that referenced a removed node/policy has
    # either been deleted above or repointed during the upsert.
    for name, pol in existing_policies.items():
        if name not in yaml_policy_names:
            session.delete(pol)
    session.flush()

    for name, node in existing_nodes.items():
        if name not in yaml_node_names:
            session.delete(node)
    session.flush()

    # Record what we loaded so drift-detection has a baseline.
    marker = session.get(Setting, SETTING_LAST_YAML_LOAD)
    payload = {"path": str(path), "sha256": sha, "at": datetime.now(UTC).isoformat()}
    if marker is None:
        session.add(Setting(key=SETTING_LAST_YAML_LOAD, value=payload))
    else:
        marker.value = payload
        marker.updated_at = datetime.now(UTC)

    log.info(
        "registry.reconciled",
        nodes=len(parsed.nodes),
        policies=len(parsed.backup_policies),
        services=len(parsed.services),
    )
    return parsed


# ---------------------------------------------------------------------------
# Dashboard-driven actions
# ---------------------------------------------------------------------------


def reload_registry_from_yaml(session: Session, path: Path) -> RegistryFile:
    """Reset button — re-runs the boot reconcile. Alias for clarity at call sites."""
    return reconcile_yaml_into_db(session, path)


def save_registry_to_yaml(session: Session, path: Path, *, actor: str = "system") -> str:
    """Save button — serialise the live DB registry back to disk. Returns sha256."""
    spec = _spec_from_db(session)
    sha = write_yaml_file(path, spec)
    marker = session.get(Setting, SETTING_LAST_YAML_SAVE)
    payload = {
        "path": str(path),
        "sha256": sha,
        "at": datetime.now(UTC).isoformat(),
        "by": actor,
    }
    if marker is None:
        session.add(Setting(key=SETTING_LAST_YAML_SAVE, value=payload))
    else:
        marker.value = payload
        marker.updated_at = datetime.now(UTC)
    # Update the "last load" hash too — the disk and the DB are now in sync.
    load_marker = session.get(Setting, SETTING_LAST_YAML_LOAD)
    if load_marker is None:
        session.add(
            Setting(
                key=SETTING_LAST_YAML_LOAD,
                value={"path": str(path), "sha256": sha, "at": payload["at"]},
            )
        )
    else:
        load_marker.value = {"path": str(path), "sha256": sha, "at": payload["at"]}
        load_marker.updated_at = datetime.now(UTC)
    log.info("registry.yaml.save", path=str(path), sha256=sha[:12], actor=actor)
    return sha


class RegistryDiff:
    """Result of :func:`compute_registry_diff`."""

    __slots__ = ("db_hash", "dirty", "yaml_hash", "yaml_path")

    def __init__(self, *, dirty: bool, db_hash: str, yaml_hash: str | None, yaml_path: str | None):
        self.dirty = dirty
        self.db_hash = db_hash
        self.yaml_hash = yaml_hash
        self.yaml_path = yaml_path


def compute_registry_diff(session: Session) -> RegistryDiff:
    """Return whether the live DB registry drifts from the last-loaded YAML.

    Cheap enough to call on every dashboard render — hashes only the serialised
    spec, not full row comparisons.
    """
    spec = _spec_from_db(session)
    db_hash = _hash_spec(spec)

    marker = session.get(Setting, SETTING_LAST_YAML_LOAD)
    if marker is None:
        return RegistryDiff(dirty=True, db_hash=db_hash, yaml_hash=None, yaml_path=None)

    yaml_path = marker.value.get("path")
    yaml_sha_disk = marker.value.get("sha256")

    # Content hash is against the DB spec — we need a stable hash of the YAML
    # in the same normalised space. Cheapest is to re-hash the current file if
    # present; if it changed on disk we mark dirty either way.
    if yaml_path and Path(yaml_path).exists():
        try:
            parsed, _ = read_yaml_file(Path(yaml_path))
            yaml_hash = _hash_spec(parsed)
        except Exception as exc:  # malformed YAML on disk => treat as dirty
            log.warning("registry.diff.yaml_unreadable", path=yaml_path, error=str(exc))
            return RegistryDiff(dirty=True, db_hash=db_hash, yaml_hash=None, yaml_path=yaml_path)
    else:
        return RegistryDiff(
            dirty=True, db_hash=db_hash, yaml_hash=yaml_sha_disk, yaml_path=yaml_path
        )

    return RegistryDiff(
        dirty=(db_hash != yaml_hash),
        db_hash=db_hash,
        yaml_hash=yaml_hash,
        yaml_path=yaml_path,
    )
