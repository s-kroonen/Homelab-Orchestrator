"""What is actually switched on for a service: its proxy hosts, its stack or container,
and (for a root service) its guest. Observed, never remembered.

A sub-service links to things that live in other systems. Whether it is *on* is read from
those systems every time (Traefik Manager, Portainer, Proxmox); the ``enabled`` flag in the
registry is only what the operator asked for. ``on``, ``off`` or ``mixed`` is derived from
the parts, and ``mixed`` always says which parts disagree. See docs/portainer_deploys.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterError, AdapterNotConfigured, AdapterUnreachable
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.portainer.links import container_url, stack_url
from orchestrator.adapters.proxy.base import RouteInfo, find_route
from orchestrator.config import Settings
from orchestrator.controllers import ControllerError, build_controller
from orchestrator.db.models import ProxyHost, Service
from orchestrator.health.guest_gate import GuestStateCache
from orchestrator.registry.proxy_push import is_live_managed, ref_of
from orchestrator.registry.tree import TreeIndex


@dataclass(frozen=True)
class Part:
    """One linked thing and what it is doing right now."""

    #: proxy | stack | container | controller | guest
    kind: str
    ref: str
    label: str
    #: True / False when it could be read, None when it could not.
    on: bool | None
    detail: str = ""
    open_url: str = ""
    #: Off by its own choice (a proxy host switched off on its own): not part of the
    #: agreement, so it never makes the service look mixed.
    deliberate: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "label": self.label,
            "on": self.on,
            "detail": self.detail,
            "open_url": self.open_url,
            "deliberate": self.deliberate,
        }


@dataclass
class LinkedState:
    slug: str
    #: What was asked for (``Service.enabled``).
    intent: bool
    #: Intent AND every ancestor's intent: should this be on?
    served: bool
    parts: list[Part] = field(default_factory=list)

    @property
    def counted(self) -> list[Part]:
        return [p for p in self.parts if not p.deliberate]

    @property
    def state(self) -> str:
        """on | off | mixed | unknown | none (nothing is linked)."""
        counted = self.counted
        if not counted:
            return "none"
        known = {p.on for p in counted if p.on is not None}
        if not known:
            return "unknown"
        if known == {True, False}:
            return "mixed"
        return "on" if known == {True} else "off"

    @property
    def disagreeing(self) -> list[Part]:
        """The parts that are not in the state the rest are in (all of the minority)."""
        counted = [p for p in self.counted if p.on is not None]
        on = [p for p in counted if p.on]
        off = [p for p in counted if not p.on]
        if not on or not off:
            return []
        return off if self.served else on

    @property
    def unknown(self) -> list[Part]:
        return [p for p in self.counted if p.on is None]

    @property
    def drift(self) -> str:
        """Why the observed state does not match what was asked for ('' = it does)."""
        state = self.state
        if state in {"none", "unknown"}:
            return ""
        if state == "mixed":
            bad = ", ".join(f"{p.label} {'on' if p.on else 'off'}" for p in self.disagreeing)
            return f"the linked parts disagree: {bad}"
        if self.served and state == "off":
            return "asked to be on, but everything linked is off"
        if not self.served and state == "on":
            return "asked to be off, but everything linked is on"
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "intent": self.intent,
            "served": self.served,
            "state": self.state,
            "drift": self.drift,
            "parts": [p.as_dict() for p in self.parts],
            "disagreeing": [p.ref for p in self.disagreeing],
            "unknown": [p.ref for p in self.unknown],
        }


@dataclass
class LinkedContext:
    """What several services share while their state is read: one tree, one route
    listing, one guest listing per node."""

    tree: TreeIndex
    routes: list[RouteInfo] | None
    #: One Proxmox read of every guest, shared with whoever else is reading this cycle.
    guest_cache: GuestStateCache | None = None
    #: Why Portainer could not be reached during this pass; later reads fail at once instead of
    #: each waiting out its own timeout.
    portainer_down: str = ""
    #: Skip controllers that run a command (docker / systemd / command over a transport): a
    #: pass that runs every few seconds or for every service must not start those.
    light: bool = False


async def gather_context(
    session: Session,
    adapters: AdapterBundle,
    guest_cache: GuestStateCache | None = None,
    *,
    light: bool = False,
) -> LinkedContext:
    routes: list[RouteInfo] | None = None
    proxy = getattr(adapters, "proxy", None)
    if proxy is not None:
        try:
            routes = await proxy.list_routes()
        except Exception:  # an unreachable proxy is "unknown", never an error here
            routes = None
    proxmox = getattr(adapters, "proxmox", None)
    if guest_cache is None and proxmox is not None:
        guest_cache = GuestStateCache(proxmox)
    return LinkedContext(
        tree=TreeIndex.build(session), routes=routes, guest_cache=guest_cache, light=light
    )


def portainer_cfg(svc: Service) -> dict[str, Any] | None:
    """The service's Portainer link (``endpoint`` + ``stack`` or ``container``), if any."""
    cfg = svc.controller or {}
    if cfg.get("type") != "portainer":
        return None
    return dict(cfg)


def proxy_hosts_of(session: Session, svc: Service) -> list[ProxyHost]:
    return list(
        session.exec(
            select(ProxyHost).where(ProxyHost.service_id == svc.id).order_by(ProxyHost.hostname)
        ).all()
    )


def _proxy_part(ph: ProxyHost, ctx: LinkedContext, served: bool) -> Part:
    if not ph.enabled:
        return Part(
            "proxy",
            ph.hostname,
            f"proxy {ph.hostname}",
            False,
            "switched off on its own",
            deliberate=True,
        )
    if not is_live_managed(ph):
        # Not written to any proxy from here: the record is all there is.
        return Part("proxy", ph.hostname, f"proxy {ph.hostname}", served, "local record only")
    if ctx.routes is None:
        return Part(
            "proxy", ph.hostname, f"proxy {ph.hostname}", None, "Traefik Manager could not be read"
        )
    route = find_route(ctx.routes, ref_of(ph), ph.hostname)
    if route is None:
        return Part(
            "proxy", ph.hostname, f"proxy {ph.hostname}", False, "no route in Traefik Manager"
        )
    return Part(
        "proxy",
        ph.hostname,
        f"proxy {ph.hostname}",
        route.enabled,
        "served" if route.enabled else "route parked",
    )


async def _portainer_part(
    adapters: AdapterBundle, cfg: dict[str, Any], ctx: LinkedContext | None = None
) -> Part:
    adapter = getattr(adapters, "portainer", None)
    eid = int(cfg["endpoint"])
    stack_name = str(cfg.get("stack") or "")
    kind = "stack" if stack_name else "container"
    name = stack_name or str(cfg.get("container") or "")
    label = f"{kind} {name}"
    if adapter is None or not adapter.configured:
        return Part(kind, name, label, None, "Portainer is not set up")
    if ctx is not None and ctx.portainer_down:
        return Part(kind, name, label, None, ctx.portainer_down)
    base = adapter.base_url
    try:
        if stack_name:
            stack = await adapter.get_stack(eid, name)
            if stack is None:
                return Part(kind, name, label, None, "not found in Portainer")
            boxes = await adapter.list_containers(eid, stack=name)
            running = sum(1 for c in boxes if c.state == "running")
            active = stack.status == "active"
            return Part(
                kind,
                name,
                label,
                active,
                f"{running}/{len(boxes)} containers running" if boxes else stack.status,
                stack_url(base, stack),
            )
        detail = await adapter.get_container(eid, name)
        if detail is None:
            return Part(kind, name, label, None, "not found in Portainer")
        c = detail.container
        return Part(
            kind,
            name,
            label,
            c.state == "running",
            c.status_text or c.state,
            container_url(base, eid, c.id),
        )
    except AdapterError as exc:
        if ctx is not None and isinstance(exc, AdapterUnreachable | AdapterNotConfigured):
            ctx.portainer_down = str(exc)
        return Part(kind, name, label, None, str(exc))


async def _controller_part(
    session: Session, adapters: AdapterBundle, settings: Settings, svc: Service
) -> Part | None:
    try:
        ctl = build_controller(settings, adapters.proxmox, session, svc)
    except ValueError as exc:
        return Part("controller", svc.slug, "controller", None, str(exc))
    if ctl is None:
        return None
    try:
        st = await ctl.status()
    except ControllerError as exc:
        return Part("controller", svc.slug, ctl.describe(), None, str(exc))
    return Part("controller", svc.slug, ctl.describe(), st.running, st.state)


async def _guest_part(
    session: Session, adapters: AdapterBundle, ctx: LinkedContext, svc: Service
) -> Part | None:
    from orchestrator.db.models import Node

    eg = ctx.tree.effective_guest(svc)
    if not eg.has_guest or svc.parent_id is not None:
        return None
    node = session.get(Node, eg.node_id) if eg.node_id is not None else None
    if node is None:
        return None
    label = f"{eg.kind.value} {eg.guest_id} on {node.name}"
    if ctx.guest_cache is None:
        return Part("guest", str(eg.guest_id), label, None, "Proxmox is not set up")
    guests, error = await ctx.guest_cache.guests()
    if guests is None:
        return Part("guest", str(eg.guest_id), label, None, error or "Proxmox could not be read")
    guest = next((g for g in guests if g.vmid == eg.guest_id and g.node == node.name), None)
    if guest is None:
        return Part("guest", str(eg.guest_id), label, None, "not found on the node")
    return Part("guest", str(eg.guest_id), label, guest.status == "running", guest.status)


async def read_linked(
    session: Session,
    adapters: AdapterBundle,
    settings: Settings,
    svc: Service,
    ctx: LinkedContext | None = None,
) -> LinkedState:
    """The observed state of everything linked to ``svc``."""
    ctx = ctx or await gather_context(session, adapters)
    served = ctx.tree.is_served(svc)
    state = LinkedState(slug=svc.slug, intent=svc.enabled, served=served)
    for ph in proxy_hosts_of(session, svc):
        state.parts.append(_proxy_part(ph, ctx, served))
    cfg = portainer_cfg(svc)
    if cfg is not None:
        state.parts.append(await _portainer_part(adapters, cfg, ctx))
    elif svc.controller and not ctx.light:
        part = await _controller_part(session, adapters, settings, svc)
        if part is not None:
            state.parts.append(part)
    guest = await _guest_part(session, adapters, ctx, svc)
    if guest is not None:
        state.parts.append(guest)
    return state
