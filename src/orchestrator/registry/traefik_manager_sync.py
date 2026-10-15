"""Maintenance-page fallback middleware, injected (never overwritten) per ``ProxyHost``.

For every ``ProxyHost`` linked to a ``Service``, this module ensures Traefik
routes it (hostname) to the orchestrator's own ``/maintenance/<slug>`` page
(``orchestrator.web.routers.maintenance``) whenever its real backend is
unreachable, using Traefik's own ``errors`` middleware:

1. **One shared backend service** (:data:`SHARED_BACKEND_SERVICE_NAME`),
   pointing at ``ORCHESTRATOR_PUBLIC_URL`` — the orchestrator's own address as
   reachable FROM THE GATEWAY VM. Created once, idempotently, and reused by
   every per-service middleware below.
2. **One middleware per service** (``maintenance-<slug>``), of Traefik's
   ``errors`` type: ``status`` covers the synthetic 5xx Traefik itself
   returns for a fully unreachable backend (no forwardAuth or health-check
   polling needed — a dead backend already reads as a 5xx to this
   middleware), ``service`` points at the shared backend above, and
   ``query`` is the literal path ``/maintenance/<slug>`` — matching the
   existing ``GET /maintenance/{service_slug}`` route exactly as it already
   exists; nothing there needs to change for this feature.
3. **Injection into existing routes**: the middleware's name is appended to
   the ``middlewares:`` list of every route whose hostname matches this
   ``ProxyHost`` — via the raw-YAML read-modify-write pair
   (``GET .../raw`` -> parse -> append if absent -> ``POST .../raw``), never
   ``POST /save``, so every other field and every other (including
   hand-authored) middleware on that route survives untouched. This is the
   one hard requirement of this feature: never overwrite an existing Traefik
   config, only ever append one list entry to it.

See ``docs/proxy_hosts.md`` for the full design, including the "service
with no router" resolution this module picks for step 1, and why.

Opt-in and a complete no-op (:func:`sync_maintenance_middleware` returns
``[]`` without making any HTTP call) when either ``TRAEFIK_MANAGER_URL`` or
``ORCHESTRATOR_PUBLIC_URL`` is unset — same convention as every other
optional integration in this app.

Lifecycle, mirroring ``orchestrator.registry.traefik_probes.sync_traefik_probe``'s
call convention exactly (same ``(hostname, service_id, old_hostname,
old_service_id)`` contract):

* **create** — ensure the shared backend + this service's middleware exist,
  inject the middleware name into every route matching the hostname.
* **rename** (same service, hostname changed) — remove the (unchanged,
  slug-named) middleware name from the OLD hostname's routes, inject it into
  the NEW hostname's routes.
* **re-link** (different service) — the middleware name is slug-based, so a
  different service means a different middleware: remove the OLD slug's
  middleware name from the hostname's routes, ensure the NEW slug's
  middleware, inject it.
* **unlink / delete** — remove the middleware name from the (old) hostname's
  routes. The middleware DEFINITION itself is deliberately left in place
  (orphaned-but-harmless) rather than deleted — see the module-level comment
  on :func:`_remove_from_hostname_routes` for why.

Every create/update/injection/removal is audit-logged by the caller (the
``proxy_host`` router), the same ``orchestrator.audit.log.record`` mechanism
every other mutation in this app uses — this module only returns
:class:`MaintenanceSyncEvent` objects describing what happened; it does not
touch the audit log itself (mirrors ``traefik_probes.sync_traefik_probe``).

Failures talking to Traefik Manager are caught here and logged, never raised
into the ``ProxyHost`` CRUD request that triggered them: this is a
side-effect of an ordinary create/update/delete, not an explicit
admin-triggered action the way Resync is, so the same fail-open philosophy
docs/proxy_hosts.md already applies to the auto-managed probe applies here —
a Traefik Manager outage must not block editing proxy hosts in the DB.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from ruamel.yaml import YAML

from orchestrator.config import Settings
from orchestrator.logging_setup import get_logger
from orchestrator.registry.traefik_manager_client import (
    TraefikManagerClient,
    TraefikManagerError,
)
from orchestrator.registry.traefik_status import extract_hostnames

log = get_logger(__name__)

#: The one shared Traefik "service" every per-service maintenance middleware
#: points at. Traefik's ``errors`` middleware invokes a ``service`` directly
#: — it does not need a router to reach it — but Traefik Manager's own
#: ``/save`` endpoint is modeled around creating a router+service PAIR, with
#: no supported way to write a bare service with no router at all.
#:
#: RESOLUTION: create a minimal, deliberately-never-matched router alongside
#: the service. Its ``httpRule`` targets a reserved, permanently-unresolvable
#: hostname (RFC 2606's ``.invalid`` TLD: "and are guaranteed not to be
#: installed" as a real DNS name, ever) rather than relying on an
#: empty/absent ``entryPoints`` value, whose exact behaviour (no binding at
#: all vs. falling back to Traefik Manager's documented HTTP default of
#: ``https``) isn't nailed down by its docs. Even if the router DOES end up
#: bound to a real entrypoint, no real request will ever carry
#: ``Host: <this hostname>``, so it can never actually serve traffic — this
#: is belt-and-suspenders against that documentation gap. See
#: docs/proxy_hosts.md for the full reasoning and alternatives considered.
SHARED_BACKEND_SERVICE_NAME = "orchestrator-maintenance-backend"
_DUMMY_ROUTER_HOSTNAME = "orchestrator-maintenance-backend.invalid"

#: Traefik's own convention for "treat a fully unreachable backend as this
#: middleware's trigger": a dead backend surfaces to the ``errors``
#: middleware as a synthetic 5xx (Traefik's own "no server available"/"bad
#: gateway" response), so the full server-error range is sufficient — no
#: forwardAuth or separate health poll needed. This matches the range
#: already used elsewhere in this app for "treat any 5xx as broken" (see
#: ``orchestrator.registry.traefik_probes``'s ``_EXPECT_STATUS`` upper bound).
_ERRORS_MIDDLEWARE_STATUS = ["500-599"]

_yaml = YAML(typ="rt")
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.preserve_quotes = True
_yaml.width = 4096


class MaintenanceSyncError(Exception):
    """A step could not be completed against Traefik Manager."""


@dataclass(frozen=True)
class MaintenanceSyncEvent:
    """One create/update/injection/removal :func:`sync_maintenance_middleware`
    performed, for the caller to audit-log (mirrors
    ``orchestrator.registry.traefik_probes.ProbeSyncEvent``)."""

    action: str  # "backend_service.created" | "middleware.created" | "middleware.updated"
    #              | "middleware.injected" | "middleware.removed"
    name: str
    details: dict[str, Any]


def maintenance_middleware_name(slug: str) -> str:
    """The stable, recognizable name a service's auto-managed maintenance
    middleware gets, e.g. ``maintenance-media-server``."""
    return f"maintenance-{slug}"


def _middleware_yaml(service_name: str, slug: str) -> str:
    """Build the raw YAML body ``POST /save-middleware`` expects for one
    service's maintenance-fallback ``errors`` middleware."""
    buf = io.StringIO()
    doc = _yaml.load(
        "errors:\n"
        "  status: []\n"
        f"  service: {service_name}\n"
        f"  query: /maintenance/{slug}\n"
    )
    doc["errors"]["status"] = list(_ERRORS_MIDDLEWARE_STATUS)
    _yaml.dump(doc, buf)
    return buf.getvalue()


def _public_target(settings: Settings) -> tuple[str, str]:
    """Split ``ORCHESTRATOR_PUBLIC_URL`` into ``(host, port)`` for ``/save``'s
    ``targetIp``/``targetPort`` fields."""
    from urllib.parse import urlsplit

    parsed = urlsplit(settings.orchestrator_public_url)
    host = parsed.hostname or ""
    port = str(parsed.port) if parsed.port else ("443" if parsed.scheme == "https" else "80")
    return host, port


async def _ensure_shared_backend_service(
    client: TraefikManagerClient, settings: Settings
) -> MaintenanceSyncEvent | None:
    """Create :data:`SHARED_BACKEND_SERVICE_NAME` if it doesn't already
    exist. Idempotent: checks ``GET /api/routes``'s ``services.http`` list
    first and makes no write at all when it's already there."""
    routes = await client.get_routes()
    services = routes.get("services") if isinstance(routes, dict) else None
    http_services = services.get("http") if isinstance(services, dict) else None
    if isinstance(http_services, list) and SHARED_BACKEND_SERVICE_NAME in http_services:
        return None

    host, port = _public_target(settings)
    await client.save_route(
        service_name=SHARED_BACKEND_SERVICE_NAME,
        protocol="http",
        target_ip=host,
        target_port=port,
        http_rule=f"Host(`{_DUMMY_ROUTER_HOSTNAME}`)",
        entry_points="",
    )
    return MaintenanceSyncEvent(
        "backend_service.created",
        SHARED_BACKEND_SERVICE_NAME,
        {"target": f"{host}:{port}"},
    )


async def _ensure_middleware(
    client: TraefikManagerClient, slug: str
) -> MaintenanceSyncEvent | None:
    """Create or update ``maintenance-<slug>``. Looked up by name in
    ``GET /api/routes``'s ``middlewares`` list to decide create vs. update
    (``isMwEdit``); content is always (re-)written so a stale ``query`` from
    a renamed service's old slug can never linger."""
    name = maintenance_middleware_name(slug)
    routes = await client.get_routes()
    middlewares = routes.get("middlewares") if isinstance(routes, dict) else None
    existing = None
    if isinstance(middlewares, list):
        existing = next(
            (m for m in middlewares if isinstance(m, dict) and m.get("name") == name), None
        )

    content = _middleware_yaml(SHARED_BACKEND_SERVICE_NAME, slug)
    await client.save_middleware(name, content, protocol="http", is_edit=existing is not None)
    return MaintenanceSyncEvent(
        "middleware.updated" if existing is not None else "middleware.created",
        name,
        {"query": f"/maintenance/{slug}", "service": SHARED_BACKEND_SERVICE_NAME},
    )


def _router_block(doc: Any) -> tuple[str, Any] | None:
    """Pull ``(router_name, router_dict)`` out of one route's raw YAML.

    ``GET .../raw`` for a single route returns ``http: {routers: {<one
    name>: {...}}}`` — exactly one entry, since it's scoped to that one
    route. Returns ``None`` if the document doesn't have the expected shape
    (defensive — a malformed/unexpected response must not corrupt the file).
    """
    http = doc.get("http") if hasattr(doc, "get") else None
    routers = http.get("routers") if http is not None and hasattr(http, "get") else None
    if not routers:
        return None
    for name, router in routers.items():
        return name, router
    return None


async def _add_middleware_to_route(
    client: TraefikManagerClient, route_id: str, middleware_name: str
) -> bool:
    """Append ``middleware_name`` to one route's ``middlewares:`` list via
    the raw read-modify-write pair. Returns ``True`` if a write happened
    (``False`` when it was already present — idempotent, no-op write)."""
    raw = await client.get_route_raw(route_id)
    content = raw.get("raw", "") if isinstance(raw, dict) else ""
    doc = _yaml.load(content)
    block = _router_block(doc)
    if block is None:
        log.warning("traefik_manager.route_raw_unexpected_shape", route_id=route_id)
        return False
    _name, router = block
    middlewares = router.get("middlewares")
    if middlewares is None:
        router["middlewares"] = [middleware_name]
    elif middleware_name not in list(middlewares):
        middlewares.append(middleware_name)
    else:
        return False  # already present — nothing to write

    buf = io.StringIO()
    _yaml.dump(doc, buf)
    await client.save_route_raw(route_id, buf.getvalue())
    return True


async def _remove_middleware_from_route(
    client: TraefikManagerClient, route_id: str, middleware_name: str
) -> bool:
    """The inverse of :func:`_add_middleware_to_route` — removes the name if
    present, same raw read-modify-write, same byte-for-byte-except-that-one-
    entry guarantee. Returns ``True`` if a write happened."""
    raw = await client.get_route_raw(route_id)
    content = raw.get("raw", "") if isinstance(raw, dict) else ""
    doc = _yaml.load(content)
    block = _router_block(doc)
    if block is None:
        return False
    _name, router = block
    middlewares = router.get("middlewares")
    if not middlewares or middleware_name not in list(middlewares):
        return False
    middlewares.remove(middleware_name)

    buf = io.StringIO()
    _yaml.dump(doc, buf)
    await client.save_route_raw(route_id, buf.getvalue())
    return True


async def _routes_matching_hostname(client: TraefikManagerClient, hostname: str) -> list[str]:
    routes = await client.get_routes()
    apps = routes.get("apps") if isinstance(routes, dict) else None
    matched: list[str] = []
    if not isinstance(apps, list):
        return matched
    target = hostname.strip().lower()
    for app in apps:
        if not isinstance(app, dict) or app.get("protocol", "http") != "http":
            continue
        rule = app.get("rule")
        route_id = app.get("id") or app.get("name")
        if (
            isinstance(rule, str)
            and isinstance(route_id, str)
            and target in extract_hostnames(rule)
        ):
            matched.append(route_id)
    return matched


async def _inject_into_hostname_routes(
    client: TraefikManagerClient, hostname: str, middleware_name: str
) -> list[MaintenanceSyncEvent]:
    events: list[MaintenanceSyncEvent] = []
    for route_id in await _routes_matching_hostname(client, hostname):
        if await _add_middleware_to_route(client, route_id, middleware_name):
            events.append(
                MaintenanceSyncEvent(
                    "middleware.injected",
                    middleware_name,
                    {"route_id": route_id, "hostname": hostname},
                )
            )
    return events


async def _remove_from_hostname_routes(
    client: TraefikManagerClient, hostname: str, middleware_name: str
) -> list[MaintenanceSyncEvent]:
    # The middleware DEFINITION is intentionally left in place here —
    # harmless (an unreferenced middleware does nothing) and simpler/safer
    # than deleting it: deleting would need to confirm no OTHER route
    # references it first (POST /delete-middleware/{name} 409s otherwise),
    # and the next create/re-link for the same slug would just recreate it
    # anyway. See docs/proxy_hosts.md.
    events: list[MaintenanceSyncEvent] = []
    for route_id in await _routes_matching_hostname(client, hostname):
        if await _remove_middleware_from_route(client, route_id, middleware_name):
            events.append(
                MaintenanceSyncEvent(
                    "middleware.removed",
                    middleware_name,
                    {"route_id": route_id, "hostname": hostname},
                )
            )
    return events


async def sync_maintenance_middleware(
    settings: Settings,
    *,
    hostname: str | None,
    service_slug: str | None,
    old_hostname: str | None = None,
    old_service_slug: str | None = None,
) -> list[MaintenanceSyncEvent]:
    """Create, update, inject or remove the maintenance-fallback middleware
    for one ``ProxyHost``. See the module docstring for the full lifecycle.

    Call with the ``ProxyHost``'s CURRENT ``(hostname, service_slug)`` and,
    when either changed, the PREVIOUS values — exactly the same calling
    convention as ``traefik_probes.sync_traefik_probe`` (slug stands in for
    that function's ``service_id``, since the middleware name is slug-based
    rather than id-based).

    No-op, returns ``[]``, when ``TRAEFIK_MANAGER_URL`` or
    ``ORCHESTRATOR_PUBLIC_URL`` is unset, or when there is nothing to do
    (everything — new and old — is unset). Every Traefik-Manager-side
    failure is caught, logged, and swallowed here — see the module
    docstring's "fail-open" note; this never raises into the ``ProxyHost``
    CRUD request that triggered it.
    """
    if not settings.traefik_manager_url.strip() or not settings.orchestrator_public_url.strip():
        return []
    if old_service_slug is None and old_hostname is None:
        old_service_slug, old_hostname = service_slug, hostname
    if service_slug is None and old_service_slug is None:
        return []

    events: list[MaintenanceSyncEvent] = []
    try:
        client = TraefikManagerClient(settings)

        moved_or_cleared = old_service_slug is not None and old_service_slug != service_slug
        renamed_in_place = (
            not moved_or_cleared
            and old_hostname is not None
            and hostname is not None
            and old_hostname != hostname
        )

        if (moved_or_cleared or renamed_in_place) and old_service_slug and old_hostname:
            old_name = maintenance_middleware_name(old_service_slug)
            events.extend(await _remove_from_hostname_routes(client, old_hostname, old_name))

        if service_slug is None or not hostname:
            return events

        backend_event = await _ensure_shared_backend_service(client, settings)
        if backend_event is not None:
            events.append(backend_event)
        mw_event = await _ensure_middleware(client, service_slug)
        if mw_event is not None:
            events.append(mw_event)
        name = maintenance_middleware_name(service_slug)
        events.extend(await _inject_into_hostname_routes(client, hostname, name))
    except TraefikManagerError as exc:
        log.warning(
            "traefik_manager.maintenance_sync_failed",
            hostname=hostname,
            service_slug=service_slug,
            error=str(exc),
        )
    return events
