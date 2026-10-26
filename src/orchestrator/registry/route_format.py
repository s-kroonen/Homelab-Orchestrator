"""Format proxy routes: rewrite hand-written / migrated routes into the standard shape.

Pull and push, one host at a time. *Pull*: read the route as Traefik Manager stores it and work
out what the standard route for that host would be (:mod:`route_standard`). *Push*: create the
standard route beside the old one, check Traefik loaded it, take the old one away, check again,
and record the new reference on the host. Anything that goes wrong puts things back:

* the old route is not touched until the new one is verified, so a failed create or verify is
  undone by removing the new one - nothing changed;
* a snapshot of the old route's stored YAML is written to ``<STATE_DIR>/route-snapshots/``
  before anything happens (and used to restore it if it vanishes half way);
* the run stops at the first host that fails (unless told to carry on).

Routes this cannot recreate without losing something (a path rule, several servers, a shared
service, a transport...) are listed as *blocked* with the reason and never touched. Disabled
hosts stay disabled: they are skipped. See docs/sources.md.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.proxy.base import RouteInfo, RouteOptions, RouteRaw, find_route
from orchestrator.adapters.proxy.verify import UNVERIFIED, verify_route
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import ProxyHost
from orchestrator.logging_setup import get_logger
from orchestrator.pipelines.steps import StepRecorder
from orchestrator.registry.config_files import same_file
from orchestrator.registry.proxy_push import (
    TRAEFIK_MANAGER_ROUTE_REF_KEY,
    audit_sync_events,
    desired_signature,
    host_enabled,
    is_live_managed,
    ref_of,
    resolve_file_for,
    served_overlay,
    services_by_id,
    slug_of,
)
from orchestrator.registry.route_standard import (
    RouteAnalysis,
    analyze,
    standard_options,
)

log = get_logger(__name__)

READY, STANDARD, SKIPPED, BLOCKED = "ready", "standard", "skipped", "blocked"
RECREATE, MOVE, UPDATE, NONE = "recreate", "move", "update", "none"


@dataclass
class FormatItem:
    hostname: str
    host_id: int | None
    service: str | None
    status: str = SKIPPED
    action: str = NONE
    reasons: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    current: dict[str, Any] = field(default_factory=dict)
    target: dict[str, Any] = field(default_factory=dict)
    dropped_middlewares: list[str] = field(default_factory=list)
    #: Hash of the route's stored YAML when planned: an apply that finds it changed refuses.
    fingerprint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "host_id": self.host_id,
            "service": self.service,
            "status": self.status,
            "action": self.action,
            "reasons": self.reasons,
            "changes": self.changes,
            "warnings": self.warnings,
            "current": self.current,
            "target": self.target,
            "dropped_middlewares": self.dropped_middlewares,
            "fingerprint": self.fingerprint,
        }


@dataclass
class FormatPlan:
    items: list[FormatItem] = field(default_factory=list)
    problem: str = ""
    #: The HSTS middleware the option attaches, and whether Traefik has it (None = unknown).
    hsts_middleware: str = ""
    hsts_available: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for i in self.items:
            counts[i.status] = counts.get(i.status, 0) + 1
        return {
            "problem": self.problem,
            "hsts": {"middleware": self.hsts_middleware, "available": self.hsts_available},
            "counts": counts,
            "items": [i.as_dict() for i in self.items],
        }


def _companion(routes: list[RouteInfo], route: RouteInfo) -> RouteInfo | None:
    want = f"{route.name}-redirect"
    return next(
        (r for r in routes if want in (r.route_id, r.name) and r.config_file == route.config_file),
        None,
    )


def _fingerprint(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _plain(catalog: Any, name: str) -> bool:
    from orchestrator.adapters.proxy.traefik_manager import is_plain_http_entrypoint

    address = next((e.address for e in (catalog.entrypoints or []) if e.name == name), "")
    return is_plain_http_entrypoint(name, address)


async def build_plan(
    session: Session,
    adapters: AdapterBundle,
    settings: Settings,
    *,
    hostnames: list[str] | None = None,
    hsts: bool | None = None,
) -> FormatPlan:
    """What formatting would do to each proxy host. Reads only. ``hsts``: True puts the HSTS
    middleware on every formatted route, False takes it off, None keeps what each route has."""
    from orchestrator.web.proxy_catalog import get_catalog

    proxy = adapters.proxy
    plan = FormatPlan()
    routes = await proxy.list_routes()
    if routes is None:
        plan.problem = "Traefik Manager is not configured or could not be read."
        return plan
    catalog = await get_catalog(settings, proxy, refresh=True)
    plan.hsts_middleware, plan.hsts_available = catalog.hsts_middleware, catalog.hsts_available
    known = await proxy.list_config_files()
    services = services_by_id(session)
    wanted = {h.strip().lower() for h in hostnames or []}
    hosts = session.exec(select(ProxyHost).order_by(ProxyHost.hostname)).all()
    with served_overlay(session):
        for ph in hosts:
            if wanted and ph.hostname.lower() not in wanted:
                continue
            plan.items.append(
                await _plan_one(
                    session, proxy, settings, catalog, known, routes, services, ph, hsts
                )
            )
    return plan


async def _plan_one(
    session: Session,
    proxy: Any,
    settings: Settings,
    catalog: Any,
    known: Any,
    routes: list[RouteInfo],
    services: dict[int, Any],
    ph: ProxyHost,
    hsts: bool | None = None,
) -> FormatItem:
    slug = slug_of(ph, services)
    item = FormatItem(ph.hostname, ph.id, slug)
    if not is_live_managed(ph):
        item.reasons.append("not a Traefik Manager route here (another provider, or no upstream)")
        return item
    if not host_enabled(ph):
        item.reasons.append("switched off - it stays off and is not touched")
        return item
    ref = ref_of(ph)
    route = find_route(routes, ref, ph.hostname) if ref else None
    if route is None:
        route = next(
            (
                r
                for r in routes
                if ph.hostname.lower() in {h.lower() for h in r.hostnames}
                and not r.name.endswith("-redirect")
            ),
            None,
        )
    if route is None:
        item.reasons.append("no route in Traefik Manager yet - push it first")
        return item
    if not route.enabled:
        item.reasons.append("the route is parked (disabled) in Traefik Manager")
        return item

    raw = await proxy.read_route_raw(route.ref, hostname=ph.hostname)
    if raw is None:
        item.status = BLOCKED
        item.reasons.append("its stored configuration could not be read from Traefik Manager")
        return item
    item.fingerprint = _fingerprint(raw.raw)
    info: RouteAnalysis = analyze(raw.raw, ph.hostname)
    if not info.simple:
        item.status = BLOCKED
        item.reasons = [
            f"left alone, it cannot be rewritten without losing something: {p}"
            for p in info.problems
        ]
        return item

    owned = proxy.owns_route_ref(route.name)
    companion = _companion(routes, route)
    target_file, problem = resolve_file_for(settings, ph, slug, known)
    if problem:
        item.status = BLOCKED
        item.reasons.append(problem)
        return item

    # HSTS: asked for (True/False) it is the route's option, attached by the adapter; left
    # alone (None) a route that has the middleware keeps it like any other it carries.
    hsts_name = catalog.hsts_middleware
    had_hsts = hsts_name in info.middlewares
    stored_hsts = RouteOptions.from_dict(ph.route_options).hsts
    # An owned route is rewritten in place (its middlewares are the adapter's to set), so the
    # HSTS it has must travel as the option; a recreated one carries it in the kept list.
    want_hsts = (stored_hsts or (owned and had_hsts)) if hsts is None else hsts
    if hsts and not had_hsts and catalog.hsts_available is False:
        item.warnings.append(
            f"the {hsts_name} middleware does not exist in Traefik, so HSTS is not added"
        )
        want_hsts = False
    options = standard_options(
        ph.route_options,
        redirect_available=catalog.redirect_available,
        hsts=want_hsts,
        skip_verify=info.skip_verify
        or RouteOptions.from_dict(ph.route_options).skip_upstream_tls_verify,
        tls_main=info.tls_main or None,
        tls_sans=info.tls_sans or None,
    )
    eps = list(ph.entry_points or []) or list(info.entry_points) or list(route.entry_points)
    if options.http_redirect:
        eps = [e for e in eps if not _plain(catalog, e)]  # the companion serves plain HTTP
    if not eps:
        eps = list(catalog.default_entrypoints)
    if not eps:
        item.status = BLOCKED
        item.reasons.append("no HTTPS entry point to bind the route to")
        return item
    resolver = (
        ph.cert_resolver or info.cert_resolver or catalog.default_cert_resolver or ""
    ).strip()
    legacy = info.legacy_middlewares if options.http_redirect else []
    maintenance = [m for m in info.middlewares if m.startswith("maintenance-")]
    kept = [
        m
        for m in info.middlewares
        if m not in legacy and m not in maintenance and not (hsts is not None and m == hsts_name)
    ]
    new_name = proxy.route_ref_for(ph.hostname)

    item.current = {
        "name": route.name,
        "file": route.config_file,
        "entry_points": list(info.entry_points or route.entry_points),
        "cert_resolver": info.cert_resolver or route.cert_resolver or "",
        "middlewares": list(info.middlewares),
        "upstream": route.upstream or info.upstream,
        "redirect_router": companion is not None,
    }
    item.target = {
        "name": route.name if owned else new_name,
        "file": target_file,
        "entry_points": eps,
        "cert_resolver": resolver,
        "middlewares": kept,
        "options": options.to_dict(),
        "upstream": ph.upstream,
    }
    item.dropped_middlewares = legacy

    if not owned:
        item.action = RECREATE
        item.changes.append(f"rewritten as {new_name} in Traefik Manager's own syntax")
    elif target_file and not same_file(route.config_file, target_file):
        item.action = MOVE
    elif (
        (options.http_redirect and companion is None)
        or (not options.http_redirect and companion is not None)
        or (hsts is not None and options.hsts != had_hsts)
    ):
        item.action = UPDATE
    else:
        item.status = STANDARD
        item.reasons.append("already in the standard shape")
        return item
    if target_file and not same_file(route.config_file, target_file):
        item.changes.append(f"moves from {route.config_file or '(default file)'} to {target_file}")
    if options.http_redirect and companion is None:
        item.changes.append("adds the HTTP -> HTTPS redirect router")
    if not options.http_redirect and catalog.redirect_available is False:
        item.warnings.append(
            f"the {catalog.redirect_middleware} middleware does not exist in Traefik, so no "
            "redirect router is added"
        )
    if hsts is not None and options.hsts != had_hsts:
        item.changes.append(
            f"adds HSTS ({hsts_name})" if options.hsts else f"removes HSTS ({hsts_name})"
        )
    if legacy:
        item.changes.append(f"drops {', '.join(legacy)} (replaced by the redirect router)")
    if sorted(eps) != sorted(info.entry_points):
        item.changes.append(
            f"entry points {', '.join(info.entry_points) or '-'} -> {', '.join(eps)}"
        )
    others = [
        r.name
        for r in routes
        if r is not route
        and r is not companion
        and ph.hostname.lower() in {h.lower() for h in r.hostnames}
    ]
    if others:
        item.warnings.append(
            f"other routers also match this host and are left as they are: {', '.join(others)}"
        )
    item.status = READY
    return item


# -- apply ------------------------------------------------------------------------------------


class FormatFailed(Exception):
    pass


@dataclass
class ItemResult:
    hostname: str
    ok: bool
    outcome: str  # formatted | unchanged | restored | failed
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "ok": self.ok,
            "outcome": self.outcome,
            "message": self.message,
        }


def snapshot_dir(settings: Settings, stamp: str) -> Path:
    path = settings.state_dir / "route-snapshots" / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_snapshot(folder: Path, hostname: str, raws: list[RouteRaw]) -> Path:
    path = folder / f"{hostname}.json"
    path.write_text(
        json.dumps(
            {
                "hostname": hostname,
                "taken": datetime.now(UTC).isoformat(),
                "routes": [
                    {"ref": r.ref, "config_file": r.config_file, "raw": r.raw} for r in raws
                ],
                "restore": "Paste each raw into Traefik Manager's raw editor for that route, "
                "or re-create the route and paste it.",
            },
            indent=2,
        )
    )
    path.chmod(0o600)
    return path


async def apply_item(
    session: Session,
    adapters: AdapterBundle,
    settings: Settings,
    item: FormatItem,
    *,
    actor: str,
    folder: Path,
    rec: StepRecorder | None = None,
    allow_unverified: bool = False,
) -> ItemResult:
    """Rewrite one host. Never raises: the outcome says what happened and what state it is in."""
    host = item.hostname

    def note(step: str, status: str, **detail: Any) -> None:
        if rec is not None:
            rec.record(f"{host}: {step}", status, detail=detail)

    if item.status != READY:
        return ItemResult(host, False, "unchanged", f"not ready ({item.status})")
    ph = session.get(ProxyHost, item.host_id) if item.host_id is not None else None
    if ph is None:
        return ItemResult(host, False, "unchanged", "the host record is gone")
    proxy = adapters.proxy
    routes = await proxy.list_routes()
    if routes is None:
        return ItemResult(host, False, "unchanged", "Traefik Manager could not be read")
    old = find_route(routes, item.current["name"], host)
    if old is None:
        return ItemResult(host, False, "unchanged", "the route is no longer in Traefik Manager")
    old_raw = await proxy.read_route_raw(old.ref, hostname=host)
    if old_raw is None or _fingerprint(old_raw.raw) != item.fingerprint:
        note("changed", "failed", error="the route changed since it was planned")
        return ItemResult(host, False, "unchanged", "the route changed since the plan - plan again")
    companion = _companion(routes, old)
    raws = [old_raw]
    if companion is not None:
        extra = await proxy.read_route_raw(companion.ref, hostname=host)
        if extra is not None:
            raws.append(extra)
    snap = _write_snapshot(folder, host, raws)
    note("snapshot", "ok", file=str(snap))

    target = item.target
    options = RouteOptions.from_dict(target["options"])
    eps, resolver, file = target["entry_points"], target["cert_resolver"], target["file"]
    new_ref = target["name"]
    try:
        if item.action == RECREATE:
            new_ref = await _recreate(
                proxy,
                old,
                host,
                ph.upstream,
                eps,
                resolver,
                options,
                file,
                target["middlewares"],
                allow_unverified,
                note,
            )
        elif item.action == MOVE:
            await proxy.move_route(
                old.ref,
                hostname=host,
                upstream=ph.upstream,
                entry_points=eps,
                cert_resolver=resolver,
                options=options,
                config_file=file,
            )
            note("move", "ok", to=file)
        else:
            await proxy.update_route(
                old.ref,
                hostname=host,
                upstream=ph.upstream,
                entry_points=eps,
                cert_resolver=resolver,
                options=options,
                previous_options=RouteOptions.from_dict(ph.route_options),
            )
            note("update", "ok")
        check = await verify_route(proxy, route_ref=new_ref, hostname=host, expect_entry_points=eps)
        if not check.ok and not (check.state == UNVERIFIED and allow_unverified):
            raise FormatFailed(
                f"Traefik did not accept the rewritten route: {check.message or check.state}"
            )
        note("verify", "ok", state=check.state)
    except Exception as exc:
        log.warning("route_format.failed", hostname=host, error=str(exc))
        note("failed", "failed", error=str(exc))
        restored = await _restore(proxy, host, ph, old, old_raw, raws, new_ref, item, note)
        audit.record(
            session,
            actor=actor,
            action="proxy_host.format_failed",
            target=f"proxy_host:{host}",
            details={"error": str(exc), "restored": restored, "snapshot": str(snap)},
        )
        session.commit()
        return ItemResult(
            host,
            False,
            "restored" if restored else "failed",
            f"{exc} - "
            + ("the old route is back as it was" if restored else f"CHECK IT: snapshot {snap}"),
        )

    ph.extra = {**(ph.extra or {}), TRAEFIK_MANAGER_ROUTE_REF_KEY: new_ref}
    ph.entry_points = list(eps)
    ph.cert_resolver = resolver
    ph.route_options = {} if options == RouteOptions() else options.to_dict()
    with served_overlay(session):
        ph.pushed_sig = desired_signature(ph, item.service)
    session.add(ph)
    if item.service:
        events = await proxy.ensure_maintenance_fallback(hostname=host, service_slug=item.service)
        audit_sync_events(session, actor, events)
    audit.record(
        session,
        actor=actor,
        action="proxy_host.formatted",
        target=f"proxy_host:{host}",
        details={
            "action": item.action,
            "from": old.name,
            "to": new_ref,
            "file": file,
            "dropped_middlewares": item.dropped_middlewares,
            "snapshot": str(snap),
        },
    )
    session.commit()
    return ItemResult(host, True, "formatted", f"{item.action}: {old.name} -> {new_ref}")


async def _recreate(
    proxy: Any,
    old: RouteInfo,
    host: str,
    upstream: str,
    eps: list[str],
    resolver: str,
    options: RouteOptions,
    file: str,
    middlewares: list[str],
    allow_unverified: bool,
    note: Any,
) -> str:
    """Create the standard route beside the old one, verify it, then take the old one away."""
    new_ref = await proxy.create_route(
        hostname=host,
        upstream=upstream,
        entry_points=eps,
        cert_resolver=resolver,
        options=options,
        config_file=file,
        **({"middlewares": middlewares} if middlewares else {}),
    )
    note("create", "ok", ref=new_ref, file=file)
    check = await verify_route(proxy, route_ref=new_ref, hostname=host, expect_entry_points=eps)
    if not check.ok and not (check.state == UNVERIFIED and allow_unverified):
        # nothing of the old route was touched: take the new one away again
        await proxy.delete_route(new_ref, with_redirect=True, hostname=host)
        raise FormatFailed(f"Traefik did not accept the new route: {check.message or check.state}")
    note("verify_new", "ok", state=check.state)
    await proxy.delete_route(old.ref, with_redirect=True, hostname=host)
    note("remove_old", "ok", ref=old.ref)
    return new_ref


async def _restore(
    proxy: Any,
    host: str,
    ph: ProxyHost,
    old: RouteInfo,
    old_raw: RouteRaw,
    raws: list[RouteRaw],
    new_ref: str,
    item: FormatItem,
    note: Any,
) -> bool:
    """Undo a failed rewrite. True when the old route is serving again as it was."""
    try:
        routes = await proxy.list_routes() or []
        if new_ref != old.name and find_route(routes, new_ref, host) is not None:
            try:
                await proxy.delete_route(new_ref, with_redirect=True, hostname=host)
                note("rollback", "ok", removed=new_ref)
            except Exception as exc:
                note("rollback", "failed", error=str(exc))
        routes = await proxy.list_routes() or []
        if find_route(routes, old.name, host) is None:
            await proxy.restore_route_raw(
                old_raw,
                name=old.name,
                hostname=host,
                upstream=item.current.get("upstream") or ph.upstream,
                entry_points=item.current.get("entry_points") or item.target["entry_points"],
                cert_resolver=item.current.get("cert_resolver") or "",
            )
            note("restore", "ok", ref=old.name)
        return find_route(await proxy.list_routes() or [], old.name, host) is not None
    except Exception as exc:
        note("restore", "failed", error=str(exc))
        return False


async def apply_plan(
    session: Session,
    adapters: AdapterBundle,
    settings: Settings,
    plan: FormatPlan,
    *,
    actor: str,
    rec: StepRecorder | None = None,
    stop_on_failure: bool = True,
    allow_unverified: bool = False,
    only: list[str] | None = None,
) -> list[ItemResult]:
    """Run every ready item of the plan (or just ``only``), in order."""
    folder = snapshot_dir(settings, datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    chosen = {h.lower() for h in only} if only else None
    results: list[ItemResult] = []
    for item in plan.items:
        if item.status != READY or (chosen is not None and item.hostname.lower() not in chosen):
            continue
        result = await apply_item(
            session,
            adapters,
            settings,
            item,
            actor=actor,
            folder=folder,
            rec=rec,
            allow_unverified=allow_unverified,
        )
        results.append(result)
        if rec is not None:
            rec.record(item.hostname, "ok" if result.ok else "failed", detail=result.as_dict())
        if not result.ok and stop_on_failure:
            break
    return results
