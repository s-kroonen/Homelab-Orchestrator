"""Push proxy hosts to Traefik Manager (the ``proxy`` :class:`~orchestrator.
registry.push.Pusher`).

The DB rows ARE the desired state. Nothing here runs on an edit; it runs on
"Save to YAML", on "Push now" (``POST /api/push``) and on
``orchestrator-cli proxy push``.

Per-host sync states (:func:`evaluate`):

``in_sync``
    The route exists and nothing we can read differs.
``pending_create``
    Live-managed (provider ``traefik`` with an upstream) but Traefik Manager
    has no route for it.
``pending_update``
    Edited here since the last push (the host's fingerprint differs from
    ``ProxyHost.pushed_sig``), or - for a host never pushed from here - the
    readable fields differ from what Traefik Manager has.
``detached``
    OUR fingerprint is unchanged (nothing edited here) but Traefik Manager's
    live state differs on what we can read back: enabled/disabled, upstream,
    entry points (and a missing HTTP -> HTTPS companion router). Someone
    changed it there.
``external``
    The hostname's route is not ours (adopted by Resync, hand-written, or no
    route reference at all). Its content is NEVER overwritten or deleted; only
    its enabled state is pushed.
``local``
    Not live-managed: another provider, or no upstream.

Apply order per host, each step idempotent: create (conflict check, rolled
back if Traefik rejects it) -> enable -> update -> maintenance fallback ->
disable LAST (read back) -> verify. ``pushed_sig`` is recorded only when every
step succeeded. One host failing never stops the others.

Config files: a NEW route is created in the file ``TRAEFIK_MANAGER_CONFIG_FILE``
(or the host's own ``route_options.config_file``) resolves to
(:func:`resolve_file_for`); an existing route stays in the file it is in, on
every update. Moving it is a separate, explicit, opt-in action
(:func:`move_to_configured_file`) - never part of a normal push.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import Session, select

from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.proxy.base import (
    ConfigFiles,
    ProxyAdapter,
    ProxyRouteNotFound,
    RouteInfo,
    RouteOptions,
    SyncEvent,
    find_route,
)
from orchestrator.adapters.proxy.verify import (
    ERROR,
    NOT_LOADED,
    UNVERIFIED,
    RouteVerification,
    bare_router_name,
    verify_route,
)
from orchestrator.audit import log as audit
from orchestrator.config import Settings, get_settings
from orchestrator.db.models import SETTING_PROXY_PENDING_DELETES, ProxyHost, Service, Setting
from orchestrator.domain.enums import RouterProvider
from orchestrator.logging_setup import get_logger
from orchestrator.registry.config_files import effective_file, resolve_config_file, same_file
from orchestrator.registry.push import PushResult, PushStatus, register_pusher

log = get_logger(__name__)

TARGET = "proxy"

#: ``ProxyHost.extra`` key a live-managed route's backend reference is stored
#: under (Traefik Manager: the route name). A hostname rename does NOT rename
#: the route, so the stored value is always used, never re-derived.
TRAEFIK_MANAGER_ROUTE_REF_KEY = "traefik_manager_route_id"

IN_SYNC = "in_sync"
PENDING_CREATE = "pending_create"
PENDING_UPDATE = "pending_update"
DETACHED = "detached"
EXTERNAL = "external"
LOCAL = "local"


# ---------------------------------------------------------------------------
# Desired state and its fingerprint
# ---------------------------------------------------------------------------


def is_live_managed(ph: ProxyHost) -> bool:
    return ph.router_provider is RouterProvider.TRAEFIK and bool((ph.upstream or "").strip())


def ref_of(ph: ProxyHost) -> str | None:
    ref = (ph.extra or {}).get(TRAEFIK_MANAGER_ROUTE_REF_KEY)
    return str(ref) if ref else None


def content_signature(ph: ProxyHost, service_slug: str | None = None) -> str:
    """Hash of everything pushed except the enabled flag (the maintenance
    fallback is part of it: it depends on the linked service)."""
    payload = {
        "hostname": ph.hostname,
        "upstream": (ph.upstream or "").strip(),
        "entry_points": sorted(ph.entry_points or []),
        "cert_resolver": (ph.cert_resolver or "").strip(),
        # Without config_file: where a NEW route goes is not part of its content,
        # and an existing route is not rewritten when only that changes.
        "route_options": RouteOptions.from_dict(ph.route_options).content_dict(),
        "service": service_slug or "",
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def desired_signature(ph: ProxyHost, service_slug: str | None = None) -> str:
    """``<content hash>:<1|0>`` - the content hash plus the enabled flag, kept
    visible so "only the switch changed" needs no content update."""
    return f"{content_signature(ph, service_slug)}:{'1' if ph.enabled else '0'}"


def fallback_signature(ph: ProxyHost, service_slug: str | None) -> str:
    """Fingerprint of "the maintenance-page fallback was applied for this
    hostname + service" - the only thing pushed for a host whose route is not
    ours (``external``) or that has no route of its own (``local``)."""
    raw = f"{ph.hostname}|{service_slug or ''}"
    return "fb:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


FALLBACK_MISSING = "maintenance page rule missing or broken on the live route"


def _bare(name: str) -> str:
    return name.split("@", 1)[0]


def fallback_missing(
    proxy: ProxyAdapter, routes: Sequence[RouteInfo], service_slug: str | None
) -> bool:
    """True when Traefik Manager LISTS the routes serving this host and at least
    one that is switched on and answers HTTPS lacks the maintenance middleware.
    Absence of information (the backend did not report middlewares, a redirect-only
    route, a disabled one) never counts - the recorded fingerprint says the push
    ran; this says whether the rule is actually THERE."""
    from orchestrator.adapters.proxy.traefik_manager import is_plain_http_entrypoint

    name = proxy.maintenance_middleware_name(service_slug) if service_slug else None
    if not name:
        return False
    for route in routes:
        if route.middlewares is None or not route.enabled or route.name.endswith("-redirect"):
            continue
        eps = route.entry_points
        if eps and all(is_plain_http_entrypoint(e) for e in eps):
            continue  # a plain-HTTP (redirect) router never needs the page
        if name not in {_bare(m) for m in route.middlewares}:
            return True
        if name in {_bare(m) for m in route.dangling_middlewares}:
            return True  # attached, but pointing at a service that does not exist
    return False


def _fallback_known(proxy: ProxyAdapter, routes: Sequence[RouteInfo]) -> bool:
    """Does the backend report each route's middlewares? Then the live check
    decides; otherwise the recorded fingerprint is all there is."""
    return bool(routes) and all(r.middlewares is not None for r in routes)


def _split_sig(sig: str) -> tuple[str, str]:
    content, _, flag = sig.partition(":")
    return content, flag


# ---------------------------------------------------------------------------
# Pending deletions (tombstones)
# ---------------------------------------------------------------------------


def pending_deletes(session: Session) -> list[dict[str, Any]]:
    row = session.get(Setting, SETTING_PROXY_PENDING_DELETES)
    items = (row.value or {}).get("items") if row is not None else None
    return [dict(i) for i in items] if isinstance(items, list) else []


def _store_deletes(session: Session, items: list[dict[str, Any]]) -> None:
    row = session.get(Setting, SETTING_PROXY_PENDING_DELETES)
    if not items:
        if row is not None:
            session.delete(row)
        return
    if row is None:
        session.add(Setting(key=SETTING_PROXY_PENDING_DELETES, value={"items": items}))
    else:
        row.value = {"items": items}
        session.add(row)


def add_pending_delete(
    session: Session,
    *,
    hostname: str,
    route_ref: str,
    with_redirect: bool,
    service_slug: str = "",
) -> None:
    items = [i for i in pending_deletes(session) if i.get("route_ref") != route_ref]
    items.append(
        {
            "hostname": hostname,
            "route_ref": route_ref,
            "with_redirect": bool(with_redirect),
            "service_slug": service_slug,
        }
    )
    _store_deletes(session, items)


def take_pending_delete(session: Session, hostname: str) -> dict[str, Any] | None:
    """Remove and return the tombstone for ``hostname`` (a host re-created
    under the same name takes its old route over instead of deleting it)."""
    items = pending_deletes(session)
    hit = next((i for i in items if i.get("hostname") == hostname), None)
    if hit is not None:
        _store_deletes(session, [i for i in items if i is not hit])
    return hit


def clear_pending_deletes(session: Session) -> None:
    _store_deletes(session, [])


# ---------------------------------------------------------------------------
# Classification (read-only)
# ---------------------------------------------------------------------------


@dataclass
class HostSync:
    state: str
    changes: list[str] = field(default_factory=list)
    #: What a push would do: create / enable / update / disable.
    actions: list[str] = field(default_factory=list)
    route: RouteInfo | None = None
    companion: RouteInfo | None = None
    #: Routes serving the hostname (external hosts toggle all of them).
    host_routes: list[RouteInfo] = field(default_factory=list)
    #: Traefik Manager's own on/off state for it, when known.
    tm: str | None = None

    @property
    def pending(self) -> bool:
        return bool(self.actions)


def _norm_upstream(url: str) -> str:
    raw = url.strip()
    parts = urlsplit(raw)
    if not parts.hostname:
        return raw.rstrip("/")
    scheme = (parts.scheme or "http").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{parts.hostname.lower()}:{port}"


def _routes_for_host(routes: Sequence[RouteInfo], hostname: str) -> list[RouteInfo]:
    host = hostname.strip().lower()
    return [r for r in routes if host in {h.lower() for h in r.hostnames}]


def _companion(routes: Sequence[RouteInfo], ref: str, main: RouteInfo | None) -> RouteInfo | None:
    from orchestrator.adapters.proxy.traefik_manager import redirect_route_id

    rid = redirect_route_id(ref)
    siblings = [r for r in routes if rid in (r.route_id, r.name)]
    beside = [r for r in siblings if main is not None and r.config_file == main.config_file]
    return (beside or siblings or [None])[0]


def _tm_state(routes: Sequence[RouteInfo]) -> str | None:
    if not routes:
        return None
    on = [r.enabled for r in routes]
    return "enabled" if all(on) else ("disabled" if not any(on) else "partial")


def _enabled_change(ph: ProxyHost, tm: str | None) -> str | None:
    want = "enabled" if ph.enabled else "disabled"
    if tm is None or tm == want:
        return None
    phrase = "partly disabled" if tm == "partial" else tm
    return f"enabled state differs: Traefik Manager has it {phrase}, we have it {want}"


def evaluate(
    proxy: ProxyAdapter,
    ph: ProxyHost,
    routes: Sequence[RouteInfo] | None,
    service_slug: str | None = None,
) -> HostSync:
    """Classify one host against what Traefik Manager listed (``None`` = it
    could not be read). Pure: no writes."""
    fb_sig = fallback_signature(ph, service_slug)
    service_slug_for_fb = None if not proxy.maintenance_fallback_available() else service_slug
    if not is_live_managed(ph):
        # No route of ours - but a hand-written one may serve the hostname and
        # still deserves the maintenance page when the service is down.
        if (
            routes is not None
            and service_slug_for_fb
            and ph.router_provider is RouterProvider.TRAEFIK
        ):
            serving = _routes_for_host(routes, ph.hostname)
            if serving and ph.enabled:
                if fallback_missing(proxy, serving, service_slug_for_fb):
                    return HostSync(LOCAL, [FALLBACK_MISSING], ["fallback"], host_routes=serving)
                if ph.pushed_sig != fb_sig and not _fallback_known(proxy, serving):
                    return HostSync(
                        LOCAL,
                        ["maintenance page fallback not applied yet"],
                        ["fallback"],
                        host_routes=serving,
                    )
        return HostSync(LOCAL)
    ref = ref_of(ph)
    want = desired_signature(ph, service_slug)
    want_content, _ = _split_sig(want)
    sig = ph.pushed_sig or ""
    opts = RouteOptions.from_dict(ph.route_options)
    owned = bool(ref) and proxy.owns_route_ref(ref or "")
    disable_too = [] if ph.enabled else ["disable"]

    if routes is None:
        if ref and not owned:
            return HostSync(EXTERNAL)
        if ref and sig == want:
            return HostSync(IN_SYNC)
        return HostSync(
            PENDING_UPDATE,
            ["not pushed yet (Traefik Manager could not be read to compare)"],
            ["update"],
        )

    host_routes = _routes_for_host(routes, ph.hostname)
    plain = [r for r in host_routes if not r.name.endswith("-redirect")]
    route = find_route(routes, ref, ph.hostname) if ref else (plain or host_routes or [None])[0]

    # --- not ours: only its enabled state is ever ours to push -----------------
    if (ref and not owned) or (not ref and route is not None):
        if route is None:
            return HostSync(
                EXTERNAL,
                ["the adopted route no longer exists in Traefik Manager"],
                [],
                host_routes=host_routes,
            )
        tm = _tm_state(host_routes)
        change = _enabled_change(ph, tm)
        changes = [change] if change else []
        actions = (["enable"] if ph.enabled else ["disable"]) if change else []
        if service_slug_for_fb and ph.enabled:
            if fallback_missing(proxy, host_routes, service_slug_for_fb):
                changes.append(FALLBACK_MISSING)
                actions.append("fallback")
            elif ph.pushed_sig != fb_sig and not _fallback_known(proxy, host_routes):
                changes.append("maintenance page fallback not applied yet")
                actions.append("fallback")
        return HostSync(EXTERNAL, changes, actions, route=route, host_routes=host_routes, tm=tm)

    # --- ours, but not in Traefik Manager yet ---------------------------------
    if route is None:
        gone = bool(sig)
        return HostSync(
            PENDING_CREATE,
            [
                (
                    "the route is gone from Traefik Manager (removed there) - it will be re-created"
                    if gone
                    else "not created in Traefik Manager yet"
                )
            ],
            ["create", *disable_too],
        )

    companion = _companion(routes, ref or "", route)
    on_routes = [route] + ([companion] if companion is not None and opts.http_redirect else [])
    tm = _tm_state(on_routes)

    diffs: list[str] = []
    content_diff = False
    if route.upstream and ph.upstream:
        theirs = {_norm_upstream(u) for u in route.upstream.split(",") if u.strip()}
        if theirs != {_norm_upstream(ph.upstream)}:
            diffs.append(
                f"upstream differs: Traefik Manager has {route.upstream}, we have {ph.upstream}"
            )
            content_diff = True
    if ph.entry_points and route.entry_points and set(route.entry_points) != set(ph.entry_points):
        diffs.append(
            "entry points differ: Traefik Manager has "
            f"{', '.join(route.entry_points)}, we have {', '.join(ph.entry_points)}"
        )
        content_diff = True
    if proxy.HAS_REDIRECT_COMPANION and opts.http_redirect and companion is None:
        diffs.append("the HTTP -> HTTPS redirect router is missing in Traefik Manager")
        content_diff = True
    enabled_diff = _enabled_change(ph, tm)

    def actions_for(content: bool) -> list[str]:
        out: list[str] = []
        if fb_missing:
            out.append("fallback")
        if enabled_diff and ph.enabled:
            out.append("enable")
        if content:
            out.append("update")
        if enabled_diff and not ph.enabled:
            out.append("disable")
        return out

    live_diffs = diffs + ([enabled_diff] if enabled_diff else [])
    fb_missing = (
        ph.enabled
        and bool(service_slug_for_fb)
        and fallback_missing(
            proxy, [route] + ([companion] if companion else []), service_slug_for_fb
        )
    )
    if fb_missing:
        live_diffs.append(FALLBACK_MISSING)
    if sig == want:
        if not live_diffs:
            return HostSync(IN_SYNC, route=route, companion=companion, tm=tm)
        return HostSync(
            DETACHED,
            live_diffs,
            actions_for(content_diff),
            route=route,
            companion=companion,
            tm=tm,
        )

    # Our fingerprint differs from the last push (or there is none).
    # No history (never pushed from here): only what is readable counts.
    edited = _split_sig(sig)[0] != want_content if sig else False
    changes = (["edited here since the last push"] if edited else []) + live_diffs
    actions = actions_for(edited or content_diff)
    if not actions:
        return HostSync(IN_SYNC, route=route, companion=companion, tm=tm)
    return HostSync(PENDING_UPDATE, changes, actions, route=route, companion=companion, tm=tm)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def move_to_configured_file(
    session: Session,
    adapters: AdapterBundle,
    settings: Settings,
    ph: ProxyHost,
    *,
    actor: str = "system",
) -> PushResult:
    """Opt-in: re-save this host's route into the file it RESOLVES to
    (``route_options.config_file`` or ``TRAEFIK_MANAGER_CONFIG_FILE``), taking it
    out of the file it is in now, and verify it afterwards. The route is
    rewritten from our record - like an update - so middlewares added by hand in
    Traefik Manager are not carried along (the maintenance-page fallback is put
    back). Never run by a normal push, and never for a route that is not ours
    (an adopted, hand-written one)."""
    host = ph.hostname

    def fail(message: str) -> PushResult:
        return PushResult(TARGET, host, "move", False, message)

    services = services_by_id(session)
    slug = slug_of(ph, services)
    ref = ref_of(ph)
    if not is_live_managed(ph) or not ref:
        return fail("only a route this orchestrator created can be moved")
    if not adapters.proxy.owns_route_ref(ref):
        return fail(
            "this route was adopted from a hand-written one - it is never moved automatically; "
            "move it in Traefik Manager"
        )
    if not ph.enabled:
        return fail("the host is disabled - enable and push it first (a parked route has no file)")
    known = await adapters.proxy.list_config_files()
    target, problem = resolve_file_for(settings, ph, slug, known)
    if problem:
        return fail(problem)
    if not target:
        return fail("no config file is configured for this host (TRAEFIK_MANAGER_CONFIG_FILE)")
    routes = await adapters.proxy.list_routes()
    if routes is None:
        return fail("Traefik Manager is not configured or could not be read")
    current = find_route(routes, ref, host)
    if current is None:
        return fail("Traefik Manager has no route for this host yet - push it first")
    if same_file(current.config_file, target):
        return PushResult(TARGET, host, "move", True, f"already in {target} - nothing to move")
    eps = list(ph.entry_points or [])
    if not eps:
        return fail("no entry point is stored for this host")
    opts = RouteOptions.from_dict(ph.route_options)
    try:
        landed = await adapters.proxy.move_route(
            ref,
            hostname=host,
            upstream=ph.upstream,
            entry_points=eps,
            cert_resolver=(ph.cert_resolver or "").strip(),
            options=opts,
            config_file=target,
        )
    except Exception as exc:
        log.warning("proxy_push.move_failed", hostname=host, error=str(exc))
        return fail(f"could not move the route: {exc}")
    if slug:
        events = await adapters.proxy.ensure_maintenance_fallback(hostname=host, service_slug=slug)
        audit_sync_events(session, actor, events)
    audit.record(
        session,
        actor=actor,
        action="proxy_host.moved",
        target=f"proxy_host:{host}",
        details={"id": ph.id, "from": current.config_file, "to": landed},
    )
    return PushResult(
        TARGET,
        host,
        "move",
        True,
        f"moved from {current.config_file or '(default file)'} to {landed} and verified",
    )


async def conflict_message(
    adapters: AdapterBundle, hostname: str, route_name: str, own_ref: str | None
) -> str | None:
    """Refuse to write over a router that is not ours: Traefik Manager (or any
    other provider Traefik runs) already serves this hostname, or already has a
    route with the name we would create."""
    from orchestrator.adapters.proxy.traefik_manager import redirect_route_id

    candidates: list[RouteInfo] = []
    for routes in (await adapters.proxy.list_routes(), await adapters.proxy.list_runtime_routers()):
        candidates.extend(routes or [])
    mine = {own_ref, redirect_route_id(own_ref)} if own_ref else set()
    for info in candidates:
        bare = bare_router_name(info.name)
        if bare in mine:
            continue
        if hostname in info.hostnames or bare == route_name:
            return (
                f'Traefik already has a router "{info.name}" for {hostname}. Refusing to '
                "overwrite a route this orchestrator did not create - use “Resync from "
                "Traefik” to adopt it, or remove/rename it in Traefik Manager first."
            )
    return None


def verification_note(verification: RouteVerification) -> str:
    if verification.state == UNVERIFIED:
        return " (could not verify: Traefik's runtime routers could not be read)"
    if verification.state == NOT_LOADED:
        return " (could not verify: Traefik has not loaded the router yet)"
    return ""


def audit_sync_events(session: Session, actor: str, events: list[SyncEvent]) -> None:
    for event in events:
        audit.record(
            session,
            actor=actor,
            action=event.action,
            target=f"traefik_manager:{event.name}",
            details=event.details,
        )


def services_by_id(session: Session) -> dict[int, Service]:
    return {s.id: s for s in session.exec(select(Service)).all() if s.id is not None}


def slug_of(ph: ProxyHost, services: dict[int, Service]) -> str | None:
    svc = services.get(ph.service_id) if ph.service_id else None
    return svc.slug if svc else None


# ---------------------------------------------------------------------------
# Config files
# ---------------------------------------------------------------------------


def resolve_file_for(
    settings: Settings,
    ph: ProxyHost,
    service_slug: str | None,
    known: ConfigFiles | None,
) -> tuple[str, str | None]:
    """``(file to create the route in, problem)`` for a host: its own
    ``route_options.config_file``, else the ``TRAEFIK_MANAGER_CONFIG_FILE``
    template. ``""`` = Traefik Manager's default file (blank template, or a
    manager with a single fixed file where the template has nothing to choose).
    ``problem`` is set when Traefik Manager would not write to that file."""
    override = RouteOptions.from_dict(ph.route_options).config_file
    try:
        wanted = resolve_config_file(
            settings.traefik_manager_config_file,
            service_slug=service_slug,
            hostname=ph.hostname,
            override=override,
        )
    except ValueError as exc:
        return "", f"cannot work out the config file: {exc}"
    return effective_file(wanted, known, explicit=bool(override))


def config_file_view(
    proxy: ProxyAdapter,
    settings: Settings,
    ph: ProxyHost,
    service_slug: str | None,
    route: RouteInfo | None,
    known: ConfigFiles | None,
) -> dict[str, Any]:
    """Where a host's route lives in Traefik Manager vs where it would be
    created: purely informational (never an error), for the list and edit form."""
    resolved, problem = resolve_file_for(settings, ph, service_slug, known)
    current = route.config_file if route is not None else ""
    ref = ref_of(ph)
    owned = bool(ref) and proxy.owns_route_ref(ref or "")
    misplaced = bool(
        route is not None and resolved and current and not same_file(current, resolved)
    )
    movable = bool(misplaced and owned and is_live_managed(ph))
    note = None
    if misplaced:
        note = f"Lives in {current}; a new route for this host would go to {resolved}."
        if not owned:
            note += " It was adopted from a hand-written route, so it is never moved automatically."
    return {
        "config_file": RouteOptions.from_dict(ph.route_options).config_file,
        "config_file_resolved": resolved,
        "config_file_problem": problem,
        "tm_config_file": current if route is not None else None,
        "config_file_misplaced": misplaced,
        "config_file_movable": movable,
        "config_file_note": note,
    }


# ---------------------------------------------------------------------------
# The pusher
# ---------------------------------------------------------------------------


def _select(hosts: Sequence[ProxyHost], only: Sequence[str] | None) -> list[ProxyHost]:
    if not only:
        return list(hosts)
    wanted = {h.strip().lower() for h in only}
    return [h for h in hosts if h.hostname.lower() in wanted]


class ProxyPusher:
    name = TARGET

    async def status(self, session: Session, adapters: AdapterBundle) -> PushStatus:
        hosts = session.exec(select(ProxyHost).order_by(ProxyHost.hostname)).all()
        services = services_by_id(session)
        routes = await adapters.proxy.list_routes()
        status = PushStatus(name=TARGET, reachable=routes is not None)
        if routes is None:
            status.message = "Traefik Manager is not configured or could not be read."
        for ph in hosts:
            hs = evaluate(adapters.proxy, ph, routes, slug_of(ph, services))
            if hs.state == LOCAL:
                continue
            entry = {"id": ph.id, "hostname": ph.hostname, "state": hs.state, "changes": hs.changes}
            if hs.state == DETACHED:
                status.detached.append(entry)
            elif hs.pending:
                status.pending.append(entry)
            else:
                status.in_sync += 1
        status.pending_deletes = pending_deletes(session)
        return status

    async def apply(
        self,
        session: Session,
        adapters: AdapterBundle,
        only: Sequence[str] | None = None,
        *,
        dry_run: bool = False,
        actor: str = "system",
        settings: Settings | None = None,
    ) -> list[PushResult]:
        settings = settings or get_settings()
        out: list[PushResult] = []
        hosts = _select(session.exec(select(ProxyHost).order_by(ProxyHost.hostname)).all(), only)
        services = services_by_id(session)
        tombstones = [
            t
            for t in pending_deletes(session)
            if not only or str(t.get("hostname", "")).lower() in {o.strip().lower() for o in only}
        ]
        routes = await adapters.proxy.list_routes()

        work = [
            (ph, hs)
            for ph in hosts
            if (hs := evaluate(adapters.proxy, ph, routes, slug_of(ph, services))).state != LOCAL
            or hs.actions
        ]
        needs = [(ph, hs) for ph, hs in work if hs.pending]
        if routes is None and (needs or tombstones):
            out.append(
                PushResult(
                    TARGET,
                    "*",
                    "connect",
                    False,
                    "Traefik Manager is not configured or could not be read - nothing was pushed.",
                )
            )
            return out

        if dry_run:
            for ph, hs in needs:
                for action in hs.actions:
                    out.append(
                        PushResult(
                            TARGET,
                            ph.hostname,
                            action,
                            True,
                            _plan_text(action, ph, hs),
                            planned=True,
                        )
                    )
            for t in tombstones:
                out.append(
                    PushResult(
                        TARGET,
                        str(t.get("hostname")),
                        "delete",
                        True,
                        f"would delete route {t.get('route_ref')} from Traefik Manager",
                        planned=True,
                    )
                )
            return out

        for ph, hs in work:
            if hs.pending:
                try:
                    await self._push_host(session, adapters, settings, ph, hs, services, actor, out)
                except Exception as exc:  # one host never stops the others
                    session.rollback()
                    log.warning("proxy_push.host_failed", hostname=ph.hostname, error=str(exc))
                    out.append(PushResult(TARGET, ph.hostname, "push", False, str(exc)))
                else:
                    session.commit()
            elif hs.state == IN_SYNC:
                want = desired_signature(ph, slug_of(ph, services))
                if ph.pushed_sig != want and hs.route is not None:
                    # Nothing to write: just remember that this is what is there.
                    ph.pushed_sig = want
                    session.add(ph)
                    session.commit()

        for t in tombstones:
            await self._push_delete(session, adapters, t, actor, out)
        return out

    # -- one host --------------------------------------------------------------

    async def _push_host(
        self,
        session: Session,
        adapters: AdapterBundle,
        settings: Settings,
        ph: ProxyHost,
        hs: HostSync,
        services: dict[int, Service],
        actor: str,
        out: list[PushResult],
    ) -> None:
        proxy = adapters.proxy
        host = ph.hostname

        def add(action: str, ok: bool, message: str = "") -> None:
            out.append(PushResult(TARGET, host, action, ok, message))

        if hs.state in (EXTERNAL, LOCAL):
            # Not our route: never touch its content - only switch it on or
            # off, and put the maintenance-page fallback on it.
            slug = slug_of(ph, services)
            done: list[str] = []
            if hs.state == EXTERNAL and ("enable" in hs.actions or "disable" in hs.actions):
                verb = "enable" if ph.enabled else "disable"
                for route in hs.host_routes or ([hs.route] if hs.route else []):
                    try:
                        await proxy.set_route_enabled(
                            route.ref, ph.enabled, with_redirect=False, hostname=host
                        )
                    except Exception as exc:
                        add(verb, False, f"could not {verb} {route.name}: {exc}")
                        return
                add(verb, True, f"{verb}d the existing Traefik Manager route (content left alone)")
                done.append(verb)
            if "fallback" in hs.actions and slug and proxy.maintenance_fallback_available():
                try:
                    events = await proxy.ensure_maintenance_fallback(
                        hostname=host, service_slug=slug, raise_errors=True
                    )
                except Exception as exc:
                    add("fallback", False, f"could not put the maintenance page rule on it: {exc}")
                    return
                audit_sync_events(session, actor, events)
                ph.pushed_sig = fallback_signature(ph, slug)
                session.add(ph)
                add("fallback", True, "maintenance page fallback applied to the existing route")
                done.append("fallback")
            self._audit(session, actor, ph, done, external=True)
            return

        slug = slug_of(ph, services)
        want = desired_signature(ph, slug)
        opts = RouteOptions.from_dict(ph.route_options)
        ref = ref_of(ph) or proxy.route_ref_for(host)
        eps = list(ph.entry_points or [])
        resolver = (ph.cert_resolver or "").strip()
        if not eps:
            # Created before entry points were stored: the configured default,
            # never left for Traefik Manager to guess.
            from orchestrator.web.proxy_catalog import get_catalog

            catalog = await get_catalog(settings, proxy)
            eps = list(catalog.default_entrypoints)
            resolver = resolver or catalog.default_cert_resolver
            if not eps:
                add(
                    "create" if hs.state == PENDING_CREATE else "update",
                    False,
                    "no entry point is stored and none could be determined from Traefik",
                )
                return

        created = False
        enabled_step = False
        updated = False
        done = []

        if hs.state == PENDING_CREATE:
            msg = await conflict_message(adapters, host, ref, None)
            if msg:
                add("create", False, msg)
                return
            config_file, problem = resolve_file_for(
                settings, ph, slug, await proxy.list_config_files()
            )
            if problem:
                add("create", False, problem)
                return
            try:
                new_ref = await proxy.create_route(
                    hostname=host,
                    upstream=ph.upstream,
                    entry_points=eps,
                    cert_resolver=resolver,
                    options=opts,
                    config_file=config_file,
                )
            except Exception as exc:
                add("create", False, f"could not create the route on Traefik Manager: {exc}")
                return
            created = True
            if new_ref != ref_of(ph):
                ph.extra = {**(ph.extra or {}), TRAEFIK_MANAGER_ROUTE_REF_KEY: new_ref}
                session.add(ph)
            ref = new_ref
            verification = await verify_route(
                proxy, route_ref=ref, hostname=host, expect_entry_points=eps
            )
            if verification.state == ERROR:
                await _rollback_created(proxy, ref, host, opts.http_redirect)
                add(
                    "create",
                    False,
                    "Traefik rejected the new route and it was removed again: "
                    + verification.message,
                )
                return
            where = f" in {config_file}" if config_file else ""
            add("create", True, f"created route {ref}{where}" + verification_note(verification))
            done.append("create")

        if ph.enabled and not created and hs.tm != "enabled" and "enable" in hs.actions:
            try:
                await proxy.set_route_enabled(
                    ref, True, with_redirect=opts.http_redirect, hostname=host
                )
            except Exception as exc:
                add("enable", False, f"could not enable the route: {exc}")
                return
            enabled_step = True
            add("enable", True, "route enabled")
            done.append("enable")

        if "update" in hs.actions and not created:
            try:
                await proxy.update_route(
                    ref,
                    hostname=host,
                    upstream=ph.upstream,
                    entry_points=eps,
                    cert_resolver=resolver,
                    options=opts,
                    previous_options=None,
                )
            except ProxyRouteNotFound:
                add(
                    "update",
                    False,
                    "the route vanished from Traefik Manager - push again to re-create it",
                )
                return
            except Exception as exc:
                add("update", False, f"could not update the route: {exc}")
                return
            updated = True
            add("update", True, "route updated")
            done.append("update")

        # Maintenance-page fallback (a route rewrite drops the middleware).
        fallback_failed = False
        if (
            slug
            and proxy.maintenance_fallback_available()
            and (created or updated or enabled_step or "fallback" in hs.actions)
            and ph.enabled
        ):
            try:
                events = await proxy.ensure_maintenance_fallback(
                    hostname=host, service_slug=slug, raise_errors=True
                )
            except Exception as exc:
                # Never swallowed: the host stays desynced and the reason is shown.
                fallback_failed = True
                add("fallback", False, f"could not put the maintenance page rule on it: {exc}")
            else:
                audit_sync_events(session, actor, events)
                if "fallback" in hs.actions:
                    add("fallback", True, "maintenance page rule attached to the live route")
                    done.append("fallback")

        if not ph.enabled:
            # Disable LAST, after anything above that needed the route live,
            # and only if something is still on (read back, not assumed).
            fresh = await proxy.list_routes()
            main = find_route(fresh, ref, host) if fresh is not None else None
            comp = _companion(fresh or [], ref, main) if main is not None else None
            still_on = (
                fresh is None
                or main is None
                or main.enabled
                or (comp is not None and opts.http_redirect and comp.enabled)
            )
            if still_on:
                try:
                    await proxy.set_route_enabled(
                        ref, False, with_redirect=opts.http_redirect, hostname=host
                    )
                except Exception as exc:
                    add("disable", False, f"could not disable the route: {exc}")
                    return
                add("disable", True, "route disabled (parked on Traefik Manager)")
                done.append("disable")

        if ph.enabled and (updated or enabled_step):
            verification = await verify_route(
                proxy, route_ref=ref, hostname=host, expect_entry_points=eps
            )
            if verification.state == ERROR:
                add(
                    "verify", False, "Traefik reports the router as broken: " + verification.message
                )
                return
            if verification.state != "ok":
                out[-1].message += verification_note(verification)

        if fallback_failed:
            # The route itself is pushed, but the host is not "in sync": keep the
            # old fingerprint so the next status still shows it as pending.
            self._audit(session, actor, ph, done)
            return
        ph.pushed_sig = want
        session.add(ph)
        self._audit(session, actor, ph, done)

    @staticmethod
    def _audit(
        session: Session, actor: str, ph: ProxyHost, done: list[str], external: bool = False
    ) -> None:
        audit.record(
            session,
            actor=actor,
            action="proxy_host.pushed",
            target=f"proxy_host:{ph.hostname}",
            details={"id": ph.id, "actions": done, "external": external},
        )

    # -- tombstones ------------------------------------------------------------

    async def _push_delete(
        self,
        session: Session,
        adapters: AdapterBundle,
        tombstone: dict[str, Any],
        actor: str,
        out: list[PushResult],
    ) -> None:
        hostname = str(tombstone.get("hostname", ""))
        ref = str(tombstone.get("route_ref", ""))
        slug = str(tombstone.get("service_slug") or "")

        def finish() -> None:
            items = [i for i in pending_deletes(session) if i.get("route_ref") != ref]
            _store_deletes(session, items)
            session.commit()

        try:
            await adapters.proxy.delete_route(
                ref, with_redirect=bool(tombstone.get("with_redirect")), hostname=hostname
            )
            message = f"deleted route {ref} from Traefik Manager"
        except ProxyRouteNotFound:
            message = f"route {ref} was already gone from Traefik Manager"
        except Exception as exc:
            out.append(
                PushResult(TARGET, hostname, "delete", False, f"could not delete {ref}: {exc}")
            )
            return
        if slug:
            events = await adapters.proxy.remove_maintenance_fallback(
                hostname=hostname, service_slug=slug
            )
            audit_sync_events(session, actor, events)
        audit.record(
            session,
            actor=actor,
            action="proxy_host.push_deleted",
            target=f"proxy_host:{hostname}",
            details={"route_ref": ref},
        )
        finish()
        out.append(PushResult(TARGET, hostname, "delete", True, message))


async def _rollback_created(
    proxy: ProxyAdapter, ref: str, hostname: str, with_redirect: bool
) -> None:
    """Best effort: take back the route we just created so a rejected create
    leaves nothing broken behind on Traefik Manager."""
    try:
        await proxy.delete_route(ref, with_redirect=with_redirect, hostname=hostname)
    except ProxyRouteNotFound:
        pass
    except Exception as exc:
        log.warning("proxy_push.rollback_failed", route_ref=ref, hostname=hostname, error=str(exc))


def _plan_text(action: str, ph: ProxyHost, hs: HostSync) -> str:
    if action == "create":
        return f"create route for {ph.hostname} -> {ph.upstream}"
    if action == "update":
        return f"update the route ({'; '.join(hs.changes) or 'push the edited settings'})"
    if action == "enable":
        return "switch the route on"
    if action == "fallback":
        return "put the maintenance page fallback on the existing route"
    return "switch the route off"


register_pusher(ProxyPusher())
