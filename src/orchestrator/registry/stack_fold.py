"""A Portainer stack is one sub-service: its containers are checks on it, not services.

Adopting a stack used to create a sub-service for the stack and a child per container, each
child with its own ``portainer`` probe and ``backup_gate``. The children are folded into the
stack: each container becomes a probe on the stack named ``container:<name>`` (kind
``portainer``, ``{"endpoint", "container"}``), keeping whether it was required, and a child
the backup waited for (``backup_gate: required``) becomes ``backup_waits`` on those probes.
Anything else the child had moves to the stack too: its other probes (renamed
``<container>:<probe>``), its proxy hosts, and the places other services named it
(``depends_on``, ``backup_requires``, a policy's ``requires``), and its database dump (the
stack's dump then names the container). Then the child is removed.

A child that holds what a check on the stack cannot is left as it is: its own ``recovery`` or
``deploy`` settings, or a dump while the stack already has one.

:func:`fold_all` runs once at startup, after the registry is read (it finds nothing to do the
next time), and writes services.yaml when it changed something. Adoption no longer creates the
children in the first place (``registry/adopt.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from orchestrator.audit import log as audit
from orchestrator.db.models import (
    BackupPolicy,
    DumpRecord,
    Probe,
    ProxyHost,
    Service,
    ServiceState,
)
from orchestrator.domain.enums import BackupGate, ProbeKind
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

PREFIX = "container:"


def container_probe_name(container: str) -> str:
    return PREFIX + container


def container_of(probe: Probe) -> str | None:
    """The container a stack's container probe checks, or None for any other probe."""
    if probe.kind is not ProbeKind.PORTAINER:
        return None
    cfg = probe.config or {}
    return str(cfg["container"]) if cfg.get("container") else None


def stack_link(svc: Service) -> dict[str, Any] | None:
    """The Portainer stack a sub-service is linked to (its controller), or None."""
    cfg = svc.controller or {}
    if cfg.get("type") != "portainer" or not cfg.get("stack"):
        return None
    return cfg


def _same_place(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return str(a.get("endpoint")) == str(b.get("endpoint")) and str(a.get("instance") or "") == str(
        b.get("instance") or ""
    )


def foldable(session: Session, stack_svc: Service) -> list[Service]:
    """The children of a stack sub-service that are one of its containers (a Portainer
    container link on the same environment), have no children of their own and hold nothing a
    check on the stack cannot (their own recovery or deploy, or a second dump)."""
    link = stack_link(stack_svc)
    if link is None or stack_svc.id is None:
        return []
    out = []
    dump_taken = bool(stack_svc.dump)
    for child in session.exec(select(Service).where(Service.parent_id == stack_svc.id)).all():
        cfg = child.controller or {}
        if cfg.get("type") != "portainer" or not cfg.get("container"):
            continue
        if not _same_place(cfg, link):
            continue
        if session.exec(select(Service).where(Service.parent_id == child.id)).first():
            continue
        if child.recovery or child.deploy:
            continue
        if child.dump:
            if dump_taken:
                continue
            dump_taken = True
        out.append(child)
    return out


@dataclass
class Folded:
    stack: str
    containers: list[str] = field(default_factory=list)
    probes: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)


def _free_name(taken: set[str], wanted: str) -> str:
    name, n = wanted, 2
    while name in taken:
        name = f"{wanted}-{n}"
        n += 1
    taken.add(name)
    return name


def fold(session: Session, stack_svc: Service, *, actor: str = "system") -> Folded | None:
    """Fold the container children of one stack sub-service into it. Commits. None when there
    was nothing to fold."""
    children = foldable(session, stack_svc)
    if not children:
        return None
    result = Folded(stack=stack_svc.slug)
    probes = session.exec(select(Probe).where(Probe.service_id == stack_svc.id)).all()
    taken = {p.name for p in probes}
    order = max((p.order for p in probes), default=-1) + 1
    renamed: dict[str, str] = {}
    for child in children:
        container = str((child.controller or {})["container"])
        waits = child.backup_gate is BackupGate.REQUIRED
        rows = session.exec(
            select(Probe).where(Probe.service_id == child.id).order_by(Probe.order)
        ).all()
        has_container_probe = any(container_of(p) == container for p in rows)
        for p in rows:
            if p.name.startswith("traefik:"):
                # Follows its proxy host, which moves to the stack below: keep its name, or
                # drop it when the stack already has the same one.
                if p.name in taken:
                    session.delete(p)
                    continue
                taken.add(p.name)
                p.service_id = stack_svc.id  # type: ignore[assignment]
                session.add(p)
                result.probes.append(p.name)
                continue
            own = container_of(p) == container
            name = _free_name(
                taken, container_probe_name(container) if own else f"{container}:{p.name}"
            )
            p.service_id = stack_svc.id  # type: ignore[assignment]
            p.name = name
            p.order = order
            p.backup_waits = waits and p.required
            p.enabled = p.enabled and child.enabled
            order += 1
            session.add(p)
            result.probes.append(name)
        if not has_container_probe:
            cfg = {k: v for k, v in (child.controller or {}).items() if k != "type"}
            name = _free_name(taken, container_probe_name(container))
            session.add(
                Probe(
                    service_id=stack_svc.id,  # type: ignore[arg-type]
                    name=name,
                    kind=ProbeKind.PORTAINER,
                    required=waits,
                    order=order,
                    config=cfg,
                    backup_waits=waits,
                )
            )
            order += 1
            result.probes.append(name)
        for ph in session.exec(select(ProxyHost).where(ProxyHost.service_id == child.id)).all():
            ph.service_id = stack_svc.id
            session.add(ph)
            result.hosts.append(ph.hostname)
        if child.dump:
            stack_svc.dump = {**child.dump, "container": child.dump.get("container") or container}
            session.add(stack_svc)
            for rec in session.exec(select(DumpRecord).where(DumpRecord.service_id == child.id)):
                rec.service_id = stack_svc.id
                rec.service_slug = stack_svc.slug
                session.add(rec)
        renamed[child.slug] = stack_svc.slug
        result.containers.append(container)
    session.flush()
    # Whatever named a child names the stack now.
    for svc in session.exec(select(Service)).all():
        for attr in ("depends_on", "backup_requires"):
            names = list(getattr(svc, attr) or [])
            if any(n in renamed for n in names):
                setattr(svc, attr, _dedupe([renamed.get(n, n) for n in names if n != svc.slug]))
                session.add(svc)
    for pol in session.exec(select(BackupPolicy)).all():
        names = list(pol.requires or [])
        if any(n in renamed for n in names):
            pol.requires = _dedupe([renamed.get(n, n) for n in names])
            session.add(pol)
    for child in children:
        state = session.get(ServiceState, child.id)
        if state is not None:
            session.delete(state)
        session.delete(child)
    audit.record(
        session,
        actor=actor,
        action="stack.folded",
        target=f"service:{stack_svc.slug}",
        details={
            "containers": result.containers,
            "removed": sorted(renamed),
            "probes": result.probes,
            "hosts": result.hosts,
        },
    )
    session.commit()
    log.info(
        "stack.folded",
        stack=stack_svc.slug,
        containers=result.containers,
        removed=sorted(renamed),
    )
    return result


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    return [n for n in names if not (n in seen or seen.add(n))]


def fold_all(session: Session, *, actor: str = "system") -> list[Folded]:
    """Fold every stack sub-service that still has container children."""
    out = []
    for svc in session.exec(select(Service).order_by(Service.slug)).all():
        if stack_link(svc) is None:
            continue
        done = fold(session, svc, actor=actor)
        if done is not None:
            out.append(done)
    return out


__all__ = [
    "PREFIX",
    "Folded",
    "container_of",
    "container_probe_name",
    "fold",
    "fold_all",
    "foldable",
    "stack_link",
]
