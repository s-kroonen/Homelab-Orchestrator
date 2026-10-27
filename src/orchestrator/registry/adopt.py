"""Adopting a Portainer stack: turn what Portainer already runs into a sub-service.

Portainer owns the stack. Adopting reads it (its containers, their published ports) and
proposes the orchestrator's side: a sub-service linked to the stack, a child per container, the
proxy hosts that point at it, probes, and a deploy method. Nothing is copied from Portainer
(no compose file, no Git URL, no environment): the link is the endpoint number and the stack
name, exactly as a service links to a guest by its VMID.

``suggest`` only reads. ``adopt`` creates the registry entries - through the same code the
service API uses, so every check applies - and undoes them if a later step fails.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.portainer.base import Container
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import ProxyHost, Service
from orchestrator.deploy import webhook as hook
from orchestrator.deploy.policy import PRESETS, normalize_deploy, preset
from orchestrator.registry.host_cascade import host_served
from orchestrator.registry.traefik_probes import sync_traefik_probe

_DB_IMAGES = (
    "postgres", "postgis", "timescale", "mysql", "mariadb", "mongo", "redis", "valkey",
    "influxdb", "cockroach", "clickhouse", "couchdb", "memcached",
)  # fmt: skip
_DB_SERVICES = {"db", "database", "postgres", "postgresql", "mysql", "mariadb", "mongo", "redis"}
_SLUG_OK = re.compile(r"[^a-z0-9_-]+")


class StackError(Exception):
    """A refusal or failure with the HTTP status the API should use."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def slugify(text: str) -> str:
    slug = _SLUG_OK.sub("-", text.strip().lower()).strip("-_")
    return (slug or "stack")[:60]


def free_slug(session: Session, wanted: str, taken: set[str] | None = None) -> str:
    used = {s.slug for s in session.exec(select(Service)).all()} | (taken or set())
    slug, n = wanted, 2
    while slug in used:
        slug = f"{wanted}-{n}"
        n += 1
    return slug


def instance_extra(adapters: AdapterBundle, instance: str) -> dict[str, str]:
    """``{"instance": id}`` for a link to a Portainer that is not the default one; nothing for the
    default (a link without an instance means the default, which stays that way because making
    another Portainer the default first pins the links that relied on it)."""
    return {"instance": instance} if instance and instance != adapters.default_portainer else {}


def linked_services(
    session: Session, endpoint_id: int, stack: str, *, instance: str = "", default: str = ""
) -> list[Service]:
    """Services whose Portainer link points at this stack (or a container in it), on this
    Portainer connection (``instance``; a link naming none is on the ``default`` one)."""
    out = []
    want = instance or default
    for svc in session.exec(select(Service)).all():
        cfg = svc.controller or {}
        if (
            cfg.get("type") == "portainer"
            and int(cfg.get("endpoint", -1)) == endpoint_id
            and str(cfg.get("stack") or "").lower() == stack.lower()
            and (str(cfg.get("instance") or "") or default) == want
        ):
            out.append(svc)
    return out


def _is_db(c: Container) -> bool:
    image = c.image.split("/")[-1].split(":")[0].lower()
    return any(image.startswith(d) for d in _DB_IMAGES) or (c.service or "").lower() in _DB_SERVICES


def _upstream_port(upstream: str) -> int | None:
    try:
        u = urlparse(upstream if "//" in upstream else f"http://{upstream}")
        return u.port or (443 if u.scheme == "https" else 80 if u.hostname else None)
    except ValueError:
        return None


@dataclass
class Suggestion:
    endpoint_id: int
    stack: str
    instance: str
    kind: str
    already_linked: list[str]
    parents: list[dict[str, Any]]
    parent: str | None
    slug: str
    name: str
    children: list[dict[str, Any]] = field(default_factory=list)
    hosts: list[dict[str, Any]] = field(default_factory=list)
    probes: list[dict[str, Any]] = field(default_factory=list)
    deploy_presets: list[str] = field(default_factory=list)
    has_git: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def parent_candidates(session: Session) -> list[dict[str, Any]]:
    """Services a stack can live under: the roots that own a guest."""
    return [
        {"slug": s.slug, "name": s.name, "guest": f"{s.guest_kind.value} {s.guest_id}"}
        for s in session.exec(select(Service).order_by(Service.slug)).all()
        if s.parent_id is None and s.guest_id is not None
    ]


async def suggest(
    session: Session,
    adapters: AdapterBundle,
    endpoint_id: int,
    stack_name: str,
    *,
    parent: str | None = None,
    instance: str | None = None,
) -> Suggestion:
    """What adopting this stack would create. Reads only."""
    adapter = adapters.portainer_for(instance)
    if adapter is None:
        raise StackError(f"there is no Portainer connection called {instance!r}", 404)
    if not adapter.configured:
        raise StackError("Portainer is not set up", 503)
    try:
        stack = await adapter.get_stack(endpoint_id, stack_name)
        if stack is None:
            raise StackError(f"no stack {stack_name!r} on Portainer environment {endpoint_id}", 404)
        boxes = await adapter.list_containers(endpoint_id, stack=stack.name)
        kind = await adapter.stack_kind(stack) if stack.id is not None else "unknown"
    except AdapterError as exc:
        raise StackError(str(exc), 502) from exc

    parents = parent_candidates(session)
    chosen = parent
    if chosen is None:
        named = [
            p["slug"]
            for p in parents
            if "portainer" in p["slug"].lower() or "portainer" in p["name"].lower()
        ]
        chosen = named[0] if named else (parents[0]["slug"] if len(parents) == 1 else None)

    slug = free_slug(session, slugify(stack.name))
    out = Suggestion(
        endpoint_id=endpoint_id,
        stack=stack.name,
        instance=adapter.instance_id,
        kind=kind,
        already_linked=[
            s.slug
            for s in linked_services(
                session,
                endpoint_id,
                stack.name,
                instance=adapter.instance_id,
                default=adapters.default_portainer,
            )
        ],
        parents=parents,
        parent=chosen,
        slug=slug,
        name=stack.name,
        has_git=bool(stack.git),
        deploy_presets=list(PRESETS),
    )
    if out.already_linked:
        out.notes.append(f"already linked to: {', '.join(out.already_linked)}")
    if not stack.managed:
        out.notes.append(
            "Portainer did not create this stack (it only sees its containers): it can be "
            "watched and stopped, but not redeployed from here."
        )
    if not stack.git:
        out.notes.append("Not deployed from Git: a deploy method (redeploy) is not possible.")

    taken = {slug}
    for c in boxes:
        base = (c.service or c.name).lower()
        child_slug = free_slug(session, slugify(f"{slug}-{base}"), taken)
        taken.add(child_slug)
        db = _is_db(c)
        out.children.append(
            {
                "container": c.name,
                "slug": child_slug,
                "name": c.service or c.name,
                "image": c.image,
                "state": c.state,
                "likely_db": db,
                "backup_gate": "required" if db else "ignore",
                "ports": [p.public for p in c.ports if p.public],
            }
        )

    published = {p.public for c in boxes for p in c.ports if p.public}
    for ph in session.exec(select(ProxyHost).order_by(ProxyHost.hostname)).all():
        port = _upstream_port(ph.upstream or "")
        if port is None or port not in published:
            continue
        owner = session.get(Service, ph.service_id) if ph.service_id else None
        out.hosts.append(
            {
                "hostname": ph.hostname,
                "upstream": ph.upstream,
                "port": port,
                "linked_to": owner.slug if owner else None,
                "name_matches": stack.name.lower().split("-")[0] in ph.hostname.lower(),
            }
        )

    out.probes.append(
        {"service": "stack", "kind": "portainer", "name": "portainer", "required": True,
         "config": {"endpoint": endpoint_id, "stack": stack.name, **instance_extra(adapters, adapter.instance_id)}}
    )  # fmt: skip
    if out.hosts:
        first = out.hosts[0]["hostname"]
        out.probes.append(
            {"service": "stack", "kind": "http", "name": "http", "required": True,
             "config": {"url": f"https://{first}", "expect_status": [200, 204, 301, 302, 303, 307, 308, 401]}}
        )  # fmt: skip
    for child in out.children:
        out.probes.append(
            {"service": child["slug"], "kind": "portainer", "name": "portainer",
             "required": child["likely_db"],
             "config": {"endpoint": endpoint_id, "container": child["container"], **instance_extra(adapters, adapter.instance_id)}}
        )  # fmt: skip
    return out


# -- adopting ---------------------------------------------------------------------------------------


@dataclass
class ChildPlan:
    container: str
    slug: str
    name: str = ""
    backup_gate: str = "ignore"


@dataclass
class AdoptPlan:
    endpoint_id: int
    stack: str
    slug: str
    parent: str
    #: The Portainer connection the stack is on (blank = the default one).
    instance: str = ""
    name: str = ""
    group: str = ""
    children: list[ChildPlan] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    probes: bool = True
    #: a preset name (none, probes, probes+backup) or None for no deploy method
    deploy: str | None = None
    #: gitlab | github | generic - also creates the webhook secret
    webhook: str | None = None


@dataclass
class AdoptResult:
    services: list[str]
    hosts: list[str]
    probes: int
    deploy: dict[str, Any]
    webhook_secret: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


async def _call(coro: Any) -> Any:
    try:
        return await coro
    except HTTPException as exc:
        raise StackError(str(exc.detail), exc.status_code) from exc


def link_host(
    session: Session, settings: Settings, actor: str, ph: ProxyHost, service: Service | None
) -> None:
    """Point a proxy host at a service (or at none). The route in Traefik Manager is updated
    by the next push, like every other proxy host edit."""
    before = ph.service_id
    ph.service_id = service.id if service else None
    session.add(ph)
    audit.record(
        session,
        actor=actor,
        action="proxy_host.linked" if service else "proxy_host.unlinked",
        target=f"proxy_host:{ph.hostname}",
        details={"service": service.slug if service else None, "from_service_id": before},
    )
    sync_traefik_probe(
        session,
        settings,
        hostname=ph.hostname,
        service_id=ph.service_id,
        old_hostname=ph.hostname,
        old_service_id=before,
        enabled=host_served(session, ph),
    )


async def adopt(
    session: Session, adapters: AdapterBundle, settings: Settings, actor: str, plan: AdoptPlan
) -> AdoptResult:
    from orchestrator.web.routers import dashboard as api

    adapter = adapters.portainer_for(plan.instance)
    if adapter is None:
        raise StackError(f"there is no Portainer connection called {plan.instance!r}", 404)
    if not adapter.configured:
        raise StackError("Portainer is not set up", 503)
    extra = instance_extra(adapters, adapter.instance_id)
    try:
        stack = await adapter.get_stack(plan.endpoint_id, plan.stack)
        boxes = await adapter.list_containers(plan.endpoint_id, stack=plan.stack) if stack else []
    except AdapterError as exc:
        raise StackError(str(exc), 502) from exc
    if stack is None:
        raise StackError(
            f"no stack {plan.stack!r} on Portainer environment {plan.endpoint_id}", 404
        )
    taken = linked_services(
        session,
        plan.endpoint_id,
        stack.name,
        instance=adapter.instance_id,
        default=adapters.default_portainer,
    )
    if taken:
        names = ", ".join(s.slug for s in taken)
        raise StackError(f"stack {stack.name!r} is already linked to {names}", 409)
    if (
        not plan.parent
        or session.exec(select(Service).where(Service.slug == plan.parent)).first() is None
    ):
        raise StackError("choose the service (the guest Portainer runs on) it lives under", 400)
    names = {c.name for c in boxes}
    for child in plan.children:
        if child.container not in names:
            raise StackError(f"stack {stack.name!r} has no container {child.container!r}", 400)
    hosts: list[ProxyHost] = []
    for hostname in plan.hosts:
        row = session.exec(select(ProxyHost).where(ProxyHost.hostname == hostname)).first()
        if row is None:
            raise StackError(f"no proxy host {hostname!r}", 404)
        hosts.append(row)
    deploy_block: dict[str, Any] = {}
    if plan.deploy is not None:
        try:
            deploy_block = preset(plan.deploy)
            if plan.webhook:
                deploy_block["trigger"]["webhook"] = {"provider": plan.webhook, "branch": ""}
            deploy_block = normalize_deploy(deploy_block)
        except ValueError as exc:
            raise StackError(str(exc), 400) from exc
        if not stack.git or not stack.managed:
            raise StackError(
                "this stack is not deployed from a Git repository in Portainer, so it has no "
                "deploy method to add",
                409,
            )

    actor_obj = SimpleNamespace(username=actor)
    created: list[str] = []
    moved: list[tuple[ProxyHost, int | None]] = []
    result = AdoptResult(services=[], hosts=[], probes=0, deploy={})
    try:
        link = {"type": "portainer", "endpoint": plan.endpoint_id, "stack": stack.name, **extra}
        await _call(
            api.create_service(
                api.ServiceCreate(
                    slug=plan.slug, name=plan.name or stack.name, group=plan.group,
                    parent=plan.parent, controller=link,
                ),
                session,
                actor_obj,
            )
        )  # fmt: skip
        created.append(plan.slug)
        for child in plan.children:
            await _call(
                api.create_service(
                    api.ServiceCreate(
                        slug=child.slug, name=child.name or child.container, group=plan.group,
                        parent=plan.slug, backup_gate=child.backup_gate,
                        controller={"type": "portainer", "endpoint": plan.endpoint_id, "container": child.container, **extra},
                    ),
                    session,
                    actor_obj,
                )
            )  # fmt: skip
            created.append(child.slug)
        stack_svc = session.exec(select(Service).where(Service.slug == plan.slug)).one()
        for ph in hosts:
            moved.append((ph, ph.service_id))
            link_host(session, settings, actor, ph, stack_svc)
            result.hosts.append(ph.hostname)
        session.commit()

        if plan.probes:
            result.probes = await _add_probes(session, api, actor_obj, plan, hosts, boxes, extra)
        if deploy_block:
            await _call(
                api.update_service(
                    plan.slug, api.ServiceUpdate(deploy=deploy_block), session, actor_obj
                )
            )
            result.deploy = deploy_block
            if plan.webhook:
                result.webhook_secret = hook.rotate_secret(session, plan.slug)
        audit.record(
            session,
            actor=actor,
            action="stack.adopted",
            target=f"service:{plan.slug}",
            details={
                "endpoint": plan.endpoint_id,
                "stack": stack.name,
                "services": created,
                "hosts": result.hosts,
                "deploy": plan.deploy,
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        for ph, old in moved:
            ph.service_id = old
            session.add(ph)
        session.commit()
        for slug in reversed(created):
            row = session.exec(select(Service).where(Service.slug == slug)).first()
            if row is not None:
                session.delete(row)
                session.commit()
        raise
    result.services = created
    return result


async def _add_probes(
    session: Session,
    api: Any,
    actor: Any,
    plan: AdoptPlan,
    hosts: list[ProxyHost],
    boxes: list[Container],
    extra: dict[str, str] | None = None,
) -> int:
    count = 0
    extra = extra or {}

    async def add(slug: str, name: str, kind: str, required: bool, config: dict[str, Any]) -> None:
        nonlocal count
        await _call(
            api.create_probe(
                slug,
                api.ProbeCreate(name=name, kind=kind, required=required, config=config),
                session,
                actor,
            )
        )
        count += 1

    await add(
        plan.slug,
        "portainer",
        "portainer",
        True,
        {"endpoint": plan.endpoint_id, "stack": plan.stack, **extra},
    )
    if hosts:
        await add(
            plan.slug, "http", "http", True,
            {"url": f"https://{hosts[0].hostname}", "expect_status": [200, 204, 301, 302, 303, 307, 308, 401]},
        )  # fmt: skip
    db_names = {c.name for c in boxes if _is_db(c)}
    for child in plan.children:
        await add(
            child.slug, "portainer", "portainer", child.container in db_names,
            {"endpoint": plan.endpoint_id, "container": child.container, **extra},
        )  # fmt: skip
    return count
