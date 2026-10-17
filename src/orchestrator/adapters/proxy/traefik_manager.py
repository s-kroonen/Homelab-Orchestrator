"""Traefik Manager (https://github.com/chr0nzz/traefik-manager)-backed
:class:`~orchestrator.adapters.proxy.base.ProxyAdapter`.

Traefik Manager runs alongside Traefik on the gateway VM and manages the
same ``/etc/traefik/dynamic/`` file-provider directory this repo's Traefik
migration already uses (see ``docs/gateway_traefik_migration.md``). It is
the **source of truth** for proxy routes, and this is the orchestrator's
client of it — the same relationship ``orchestrator.adapters`` has with
Proxmox/IPMI.

This used to be two standalone modules
(``orchestrator.registry.traefik_manager_client`` — a thin REST client, and
``traefik_manager_sync`` — the maintenance-middleware feature built on top
of it) kept deliberately OUTSIDE ``orchestrator.adapters``, reasoned at the
time as "nothing to dry-run/start/stop here, just short-lived HTTP calls."
That reasoning no longer holds now that the actual requirement is multiple
interchangeable proxy backends (today: Traefik Manager; someday: bare
Traefik, NPM, Pangolin) — see ``docs/roadmap.md``. Both modules' real logic
now lives here, behind :class:`ProxyAdapter`.

:meth:`TraefikManagerProxyAdapter.start`/:meth:`stop` are still no-ops —
that original reasoning (no persistent connection; every call here is a
short-lived ``httpx.AsyncClient`` built fresh, same as
``orchestrator.registry.traefik_status``'s per-request client) is still
correct, it just no longer implies "don't make this an adapter." The base
class's default no-op lifecycle covers this; nothing is overridden here.

Authentication is always ``X-Api-Key`` (generated per-device in Traefik
Manager's own UI: Settings -> Authentication -> API Keys). API-key auth
bypasses Traefik Manager's CSRF checks entirely (see its docs' API
reference), so no CSRF token handling is needed here. Every mutating form
endpoint is called with ``X-Requested-With: fetch`` so Traefik Manager
answers with JSON instead of a 302 redirect to its own UI (see its API
docs' "Response format" section) — without that header these endpoints
assume a browser and never return the JSON body this adapter expects.

Failure handling, preserved exactly from the old split:

* :meth:`list_routes` (the passive drift-detection/Resync read path) never
  raises — every failure degrades to ``None``, mirroring
  ``orchestrator.registry.traefik_status``'s silent-degrade convention. A
  caller that needs a hard failure instead (Resync) turns that ``None``
  into its own error.
* :meth:`ensure_maintenance_fallback`/:meth:`remove_maintenance_fallback`
  (genuine *writes*) also never raise into the caller — a side effect of an
  ordinary ``ProxyHost`` create/update/delete must not fail that request
  just because Traefik Manager is unreachable (see ``docs/proxy_hosts.md``'s
  "Fail-open, by design"). Every failure is caught here, logged, and
  swallowed; the caller gets ``[]`` back, same as before.
* :meth:`create_route`/:meth:`update_route`/:meth:`delete_route` (the
  general route CRUD backing a live-managed ``ProxyHost``) are the
  opposite: these ARE the explicit action an admin just took, so every
  failure RAISES into the caller (``web/routers/proxy_hosts.py``) —
  nothing here is caught or swallowed — except that a 404 from Traefik
  Manager is re-raised as the backend-neutral
  :class:`~orchestrator.adapters.proxy.base.ProxyRouteNotFound` rather than
  :class:`TraefikManagerNotFound` directly, so that one call site doesn't
  need to import this module's own exception hierarchy.
"""

from __future__ import annotations

import io
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from ruamel.yaml import YAML

from orchestrator.adapters.proxy.base import ProxyAdapter, ProxyRouteNotFound, RouteInfo, SyncEvent
from orchestrator.config import Settings
from orchestrator.logging_setup import get_logger
from orchestrator.registry.traefik_status import extract_hostnames

log = get_logger(__name__)


class TraefikManagerError(Exception):
    """Base class for every failure this adapter raises internally."""


class TraefikManagerNotConfigured(TraefikManagerError):
    """``TRAEFIK_MANAGER_URL`` is unset. Distinct from a real failure so
    internal callers can tell "not set up" apart from "set up but broken"."""


class TraefikManagerUnavailable(TraefikManagerError):
    """Network error, timeout, or a non-2xx/non-404 response."""


class TraefikManagerNotFound(TraefikManagerError):
    """A 404 from Traefik Manager — e.g. a route id does not exist."""


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
#: forwardAuth or separate health poll needed.
_ERRORS_MIDDLEWARE_STATUS = ["500-599"]

_yaml = YAML(typ="rt")
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.preserve_quotes = True
_yaml.width = 4096


def maintenance_middleware_name(slug: str) -> str:
    """The stable, recognizable name a service's auto-managed maintenance
    middleware gets, e.g. ``maintenance-media-server``."""
    return f"maintenance-{slug}"


def route_name_for_hostname(hostname: str) -> str:
    """The Traefik Manager route (``serviceName``/router) name this adapter
    creates for a ``ProxyHost``'s live-managed route, e.g.
    ``orchestrator-app-example-com`` for ``app.example.com``.

    Deterministic and derived from the hostname alone — never from the
    ``ProxyHost.id`` — so it reads clearly in Traefik Manager's own UI and
    so re-deriving it (e.g. for a backfill) needs no extra lookup. Prefixed
    with ``orchestrator-`` so it can never collide with a hand-authored
    router name. This IS the opaque reference :meth:`TraefikManagerProxyAdapter.
    create_route` returns and that gets persisted to ``ProxyHost.extra``
    (see ``docs/proxy_hosts.md``) — callers should treat it as opaque and
    always use the stored value rather than recomputing this, since a
    hostname rename does NOT rename the underlying route (see
    :meth:`TraefikManagerProxyAdapter.update_route`).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", hostname.strip().lower()).strip("-")
    return f"orchestrator-{slug}" if slug else "orchestrator-route"


def _split_host_port(url: str) -> tuple[str, str]:
    """Split a ``scheme://host:port`` URL into ``(host, port)`` — shared by
    the maintenance-fallback backend target and the general route CRUD
    below. Returns ``("", "")`` for an unparseable/hostless URL; callers
    decide whether that's acceptable."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if not host:
        return "", ""
    port = str(parsed.port) if parsed.port else ("443" if parsed.scheme == "https" else "80")
    return host, port


def _routes_to_route_info(apps: Any) -> list[RouteInfo]:
    """Convert Traefik Manager's ``GET /api/routes`` ``apps`` list into
    :class:`RouteInfo`. HTTP routes only — ``protocol`` other than
    ``"http"`` (tcp, udp) is skipped, since drift detection is HTTP-only.
    Traefik Manager already resolves each route's backend servers, so this
    needs no second call the way the direct-Traefik-API path does to
    resolve a router's ``service`` -> backend URL."""
    infos: list[RouteInfo] = []
    if not isinstance(apps, list):
        return infos
    for app in apps:
        if not isinstance(app, dict) or app.get("protocol", "http") != "http":
            continue
        name = app.get("name") or app.get("id")
        rule = app.get("rule")
        if not isinstance(name, str) or not isinstance(rule, str):
            continue
        hostnames = extract_hostnames(rule)
        if not hostnames:
            continue
        servers = app.get("servers")
        upstream = (
            ",".join(s for s in servers if isinstance(s, str))
            if isinstance(servers, list) and servers
            else (app.get("target") if isinstance(app.get("target"), str) else None)
        )
        infos.append(RouteInfo(name=name, hostnames=hostnames, upstream=upstream or None))
    return infos


class TraefikManagerProxyAdapter(ProxyAdapter):
    """One adapter instance per process; each call still builds its own
    short-lived ``httpx.AsyncClient`` (see module docstring) — mirrors
    ``orchestrator.registry.traefik_status``'s per-request client rather
    than a long-lived singleton, so a settings change takes effect
    immediately and no connection pool outlives a single call."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # -- low-level HTTP ---------------------------------------------------

    def _require_configured(self) -> str:
        base_url = self._settings.traefik_manager_url.strip()
        if not base_url:
            raise TraefikManagerNotConfigured("TRAEFIK_MANAGER_URL is not set")
        return base_url.rstrip("/")

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._require_configured(),
            verify=self._settings.traefik_manager_verify_tls,
            timeout=self._settings.traefik_manager_timeout_s,
            headers={"X-Api-Key": self._settings.traefik_manager_api_key.get_secret_value()},
        )

    @staticmethod
    def _raise_for(exc: Exception, *, url: str) -> None:
        if isinstance(exc, httpx.TimeoutException):
            log.warning("traefik_manager.timeout", url=url, error=str(exc))
            raise TraefikManagerUnavailable(f"Traefik Manager timed out: {exc}") from exc
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if status_code == 404:
                raise TraefikManagerNotFound(f"{url}: not found") from exc
            log.warning("traefik_manager.http_error", url=url, status=status_code)
            raise TraefikManagerUnavailable(
                f"Traefik Manager returned {status_code} for {url}"
            ) from exc
        if isinstance(exc, httpx.HTTPError):
            log.warning("traefik_manager.request_failed", url=url, error=str(exc))
            raise TraefikManagerUnavailable(f"could not reach Traefik Manager: {exc}") from exc
        raise exc

    async def _get_json(self, client: httpx.AsyncClient, path: str) -> dict[str, Any]:
        try:
            response = await client.get(path)
            response.raise_for_status()
            return response.json()
        except ValueError as exc:  # non-JSON body
            raise TraefikManagerUnavailable(f"{path}: non-JSON response") from exc
        except Exception as exc:
            self._raise_for(exc, url=path)
            raise  # pragma: no cover - _raise_for always raises

    async def _post_json(self, client: httpx.AsyncClient, path: str, **kwargs: Any) -> Any:
        try:
            response = await client.post(path, **kwargs)
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()
        except ValueError:
            # Some success responses (e.g. a 302-turned-200 edge case) may not
            # carry JSON; a 2xx without a body is still success.
            return {}
        except Exception as exc:
            self._raise_for(exc, url=path)
            raise  # pragma: no cover

    # -- Traefik Manager REST calls ----------------------------------------

    async def _get_routes(self) -> dict[str, Any]:
        """``GET /api/routes`` — every managed route, middleware and service
        name from every loaded config file. See Traefik Manager's API docs."""
        async with self._client() as client:
            return await self._get_json(client, "/api/routes")

    async def _get_route_raw(self, route_id: str) -> dict[str, Any]:
        """``GET /api/routes/{route_id}/raw`` — the route's YAML as stored.

        ``{"raw": "...", "configFile": "...", "proto": "http"}``. Raises
        :class:`TraefikManagerNotFound` if no config file contains that
        router.
        """
        async with self._client() as client:
            return await self._get_json(client, f"/api/routes/{route_id}/raw")

    async def _save_route_raw(self, route_id: str, content: str) -> None:
        """``POST /api/routes/{route_id}/raw`` — replace that route's YAML
        wholesale. JSON body, unlike every other write endpoint here (which
        are form-urlencoded) — see Traefik Manager's OpenAPI spec."""
        async with self._client() as client:
            await self._post_json(
                client,
                f"/api/routes/{route_id}/raw",
                json={"content": content},
                headers={"X-Requested-With": "fetch"},
            )

    async def _save_route(
        self,
        *,
        service_name: str,
        protocol: str = "http",
        target_ip: str = "",
        target_port: str = "",
        subdomain: str = "",
        http_rule: str = "",
        entry_points: str = "",
        middlewares: str = "",
        is_edit: bool = False,
        original_id: str = "",
        config_file: str = "",
    ) -> None:
        """``POST /save`` — create or update a route (router + service).

        Only the fields this integration actually needs are exposed; see
        Traefik Manager's API docs for the full field list.
        """
        data: dict[str, str] = {
            "serviceName": service_name,
            "protocol": protocol,
            "targetIp": target_ip,
            "targetPort": target_port,
            "subdomain": subdomain,
            "httpRule": http_rule,
            "entryPoints": entry_points,
            "middlewares": middlewares,
            "isEdit": "true" if is_edit else "false",
            "originalId": original_id,
            "configFile": config_file,
        }
        async with self._client() as client:
            await self._post_json(client, "/save", data=data, headers={"X-Requested-With": "fetch"})

    async def _delete_route(self, route_id: str) -> None:
        """``POST /delete/{route_id}`` — delete a route (router + its
        service, unless another router still references it). Form-
        urlencoded, per Traefik Manager's API docs. Raises
        :class:`TraefikManagerNotFound` (via ``_raise_for``) on a 404 —
        "no config file and no disabled-route record holds that router"."""
        async with self._client() as client:
            await self._post_json(
                client, f"/delete/{route_id}", data={}, headers={"X-Requested-With": "fetch"}
            )

    async def _save_middleware(
        self,
        name: str,
        content: str,
        *,
        protocol: str = "http",
        is_edit: bool = False,
        original_id: str = "",
        config_file: str = "",
    ) -> None:
        """``POST /save-middleware`` — create or update a middleware.

        Form-urlencoded, per Traefik Manager's API docs — ``middlewareContent``
        is the middleware's raw YAML body.
        """
        data = {
            "middlewareName": name,
            "middlewareContent": content,
            "mwProtocol": protocol,
            "isMwEdit": "true" if is_edit else "false",
            "originalMwId": original_id,
            "configFile": config_file,
        }
        async with self._client() as client:
            await self._post_json(
                client, "/save-middleware", data=data, headers={"X-Requested-With": "fetch"}
            )

    # -- ProxyAdapter: read path --------------------------------------------

    async def list_routes(self) -> list[RouteInfo] | None:
        if not self._settings.traefik_manager_url.strip():
            return None
        try:
            response = await self._get_routes()
        except TraefikManagerNotConfigured:
            return None
        except TraefikManagerError as exc:
            log.warning("traefik_manager.poll_failed", error=str(exc))
            return None
        apps = response.get("apps") if isinstance(response, dict) else None
        return _routes_to_route_info(apps)

    # -- ProxyAdapter: maintenance-fallback write path ----------------------

    def _public_target(self) -> tuple[str, str]:
        """Split ``ORCHESTRATOR_PUBLIC_URL`` into ``(host, port)`` for
        ``/save``'s ``targetIp``/``targetPort`` fields."""
        return _split_host_port(self._settings.orchestrator_public_url)

    @staticmethod
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

    async def _ensure_shared_backend_service(self) -> SyncEvent | None:
        """Create :data:`SHARED_BACKEND_SERVICE_NAME` if it doesn't already
        exist. Idempotent: checks ``GET /api/routes``'s ``services.http``
        list first and makes no write at all when it's already there."""
        routes = await self._get_routes()
        services = routes.get("services") if isinstance(routes, dict) else None
        http_services = services.get("http") if isinstance(services, dict) else None
        if isinstance(http_services, list) and SHARED_BACKEND_SERVICE_NAME in http_services:
            return None

        host, port = self._public_target()
        await self._save_route(
            service_name=SHARED_BACKEND_SERVICE_NAME,
            protocol="http",
            target_ip=host,
            target_port=port,
            http_rule=f"Host(`{_DUMMY_ROUTER_HOSTNAME}`)",
            entry_points="",
        )
        return SyncEvent(
            "backend_service.created",
            SHARED_BACKEND_SERVICE_NAME,
            {"target": f"{host}:{port}"},
        )

    async def _ensure_middleware(self, slug: str) -> SyncEvent | None:
        """Create or update ``maintenance-<slug>``. Looked up by name in
        ``GET /api/routes``'s ``middlewares`` list to decide create vs.
        update (``isMwEdit``); content is always (re-)written so a stale
        ``query`` from a renamed service's old slug can never linger."""
        name = maintenance_middleware_name(slug)
        routes = await self._get_routes()
        middlewares = routes.get("middlewares") if isinstance(routes, dict) else None
        existing = None
        if isinstance(middlewares, list):
            existing = next(
                (m for m in middlewares if isinstance(m, dict) and m.get("name") == name), None
            )

        content = self._middleware_yaml(SHARED_BACKEND_SERVICE_NAME, slug)
        await self._save_middleware(name, content, protocol="http", is_edit=existing is not None)
        return SyncEvent(
            "middleware.updated" if existing is not None else "middleware.created",
            name,
            {"query": f"/maintenance/{slug}", "service": SHARED_BACKEND_SERVICE_NAME},
        )

    @staticmethod
    def _router_block(doc: Any) -> tuple[str, Any] | None:
        """Pull ``(router_name, router_dict)`` out of one route's raw YAML.

        ``GET .../raw`` for a single route returns ``http: {routers: {<one
        name>: {...}}}`` — exactly one entry, since it's scoped to that one
        route. Returns ``None`` if the document doesn't have the expected
        shape (defensive — a malformed/unexpected response must not corrupt
        the file).
        """
        http = doc.get("http") if hasattr(doc, "get") else None
        routers = http.get("routers") if http is not None and hasattr(http, "get") else None
        if not routers:
            return None
        for name, router in routers.items():
            return name, router
        return None

    async def _add_middleware_to_route(self, route_id: str, middleware_name: str) -> bool:
        """Append ``middleware_name`` to one route's ``middlewares:`` list
        via the raw read-modify-write pair. Returns ``True`` if a write
        happened (``False`` when it was already present — idempotent,
        no-op write)."""
        raw = await self._get_route_raw(route_id)
        content = raw.get("raw", "") if isinstance(raw, dict) else ""
        doc = _yaml.load(content)
        block = self._router_block(doc)
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
        await self._save_route_raw(route_id, buf.getvalue())
        return True

    async def _remove_middleware_from_route(self, route_id: str, middleware_name: str) -> bool:
        """The inverse of :meth:`_add_middleware_to_route` — removes the
        name if present, same raw read-modify-write, same
        byte-for-byte-except-that-one-entry guarantee. Returns ``True`` if a
        write happened."""
        raw = await self._get_route_raw(route_id)
        content = raw.get("raw", "") if isinstance(raw, dict) else ""
        doc = _yaml.load(content)
        block = self._router_block(doc)
        if block is None:
            return False
        _name, router = block
        middlewares = router.get("middlewares")
        if not middlewares or middleware_name not in list(middlewares):
            return False
        middlewares.remove(middleware_name)

        buf = io.StringIO()
        _yaml.dump(doc, buf)
        await self._save_route_raw(route_id, buf.getvalue())
        return True

    async def _routes_matching_hostname(self, hostname: str) -> list[str]:
        routes = await self._get_routes()
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
        self, hostname: str, middleware_name: str
    ) -> list[SyncEvent]:
        events: list[SyncEvent] = []
        for route_id in await self._routes_matching_hostname(hostname):
            if await self._add_middleware_to_route(route_id, middleware_name):
                events.append(
                    SyncEvent(
                        "middleware.injected",
                        middleware_name,
                        {"route_id": route_id, "hostname": hostname},
                    )
                )
        return events

    async def _remove_from_hostname_routes(
        self, hostname: str, middleware_name: str
    ) -> list[SyncEvent]:
        # The middleware DEFINITION is intentionally left in place here —
        # harmless (an unreferenced middleware does nothing) and simpler/safer
        # than deleting it: deleting would need to confirm no OTHER route
        # references it first (POST /delete-middleware/{name} 409s otherwise),
        # and the next create/re-link for the same slug would just recreate it
        # anyway. See docs/proxy_hosts.md.
        events: list[SyncEvent] = []
        for route_id in await self._routes_matching_hostname(hostname):
            if await self._remove_middleware_from_route(route_id, middleware_name):
                events.append(
                    SyncEvent(
                        "middleware.removed",
                        middleware_name,
                        {"route_id": route_id, "hostname": hostname},
                    )
                )
        return events

    async def ensure_maintenance_fallback(
        self, *, hostname: str, service_slug: str
    ) -> list[SyncEvent]:
        if not self._settings.traefik_manager_url.strip() or (
            not self._settings.orchestrator_public_url.strip()
        ):
            return []
        events: list[SyncEvent] = []
        try:
            backend_event = await self._ensure_shared_backend_service()
            if backend_event is not None:
                events.append(backend_event)
            mw_event = await self._ensure_middleware(service_slug)
            if mw_event is not None:
                events.append(mw_event)
            name = maintenance_middleware_name(service_slug)
            events.extend(await self._inject_into_hostname_routes(hostname, name))
        except TraefikManagerError as exc:
            log.warning(
                "traefik_manager.maintenance_sync_failed",
                hostname=hostname,
                service_slug=service_slug,
                error=str(exc),
            )
        return events

    async def remove_maintenance_fallback(
        self, *, hostname: str, service_slug: str
    ) -> list[SyncEvent]:
        if not self._settings.traefik_manager_url.strip():
            return []
        name = maintenance_middleware_name(service_slug)
        try:
            return await self._remove_from_hostname_routes(hostname, name)
        except TraefikManagerError as exc:
            log.warning(
                "traefik_manager.maintenance_sync_failed",
                hostname=hostname,
                service_slug=service_slug,
                error=str(exc),
            )
            return []

    # -- ProxyAdapter: general route CRUD -----------------------------------
    #
    # Unlike every write path above, these three RAISE on failure — see
    # ``orchestrator.adapters.proxy.base.ProxyAdapter``'s docstring for why.
    # Each route is created/edited via the same ``POST /save`` form
    # endpoint the maintenance-fallback shared backend service already
    # uses (see ``_save_route`` above), always with an explicit ``httpRule``
    # (never ``subdomain``) so the rule written is pinned down regardless
    # of Traefik Manager's own "configured domain(s)" settings. The route's
    # name (``serviceName``/router id) is :func:`route_name_for_hostname`,
    # derived from the hostname at CREATE time and then persisted by the
    # caller (``ProxyHost.extra``) — a later rename does NOT rename the
    # underlying route; :meth:`update_route` reuses the stored ``route_ref``
    # as both ``serviceName`` and ``originalId`` and just rewrites its rule.

    @staticmethod
    def _target_or_raise(upstream: str) -> tuple[str, str]:
        host, port = _split_host_port(upstream)
        if not host:
            raise ValueError(
                f"upstream {upstream!r} is not a valid URL with a host "
                "(expected e.g. http://10.0.0.5:8080)"
            )
        return host, port

    async def create_route(self, *, hostname: str, upstream: str) -> str:
        host, port = self._target_or_raise(upstream)
        name = route_name_for_hostname(hostname)
        await self._save_route(
            service_name=name,
            protocol="http",
            target_ip=host,
            target_port=port,
            http_rule=f"Host(`{hostname}`)",
            is_edit=False,
        )
        return name

    async def update_route(self, route_ref: str, *, hostname: str, upstream: str) -> None:
        host, port = self._target_or_raise(upstream)
        try:
            await self._save_route(
                service_name=route_ref,
                protocol="http",
                target_ip=host,
                target_port=port,
                http_rule=f"Host(`{hostname}`)",
                is_edit=True,
                original_id=route_ref,
            )
        except TraefikManagerNotFound as exc:
            raise ProxyRouteNotFound(str(exc)) from exc

    async def delete_route(self, route_ref: str) -> None:
        try:
            await self._delete_route(route_ref)
        except TraefikManagerNotFound as exc:
            raise ProxyRouteNotFound(str(exc)) from exc

    # -- ProxyAdapter: access-log read path (GeoIP traffic visibility) ------
    #
    # ``GET /api/traefik/logs?lines=N`` (max 1000 per Traefik Manager's own
    # API docs) — Traefik Manager already tails Traefik's own access log for
    # its own Logs tab; this reuses that exact read rather than this app
    # gaining any new SSH/filesystem access of its own. Requires an access
    # log path configured on the gateway (``ACCESS_LOG_PATH`` or Traefik
    # Manager's Settings) — without one, Traefik Manager itself answers with
    # no lines (never an error we need to special-case here beyond the usual
    # degrade-to-None).

    async def tail_access_log(self, lines: int = 200) -> list[str] | None:
        if not self._settings.traefik_manager_url.strip():
            return None
        try:
            async with self._client() as client:
                response = await self._get_json(
                    client, f"/api/traefik/logs?lines={max(1, int(lines))}"
                )
        except TraefikManagerNotConfigured:
            return None
        except TraefikManagerError as exc:
            log.warning("traefik_manager.tail_access_log_failed", error=str(exc))
            return None
        raw_lines = response.get("lines") if isinstance(response, dict) else None
        if not isinstance(raw_lines, list):
            return []
        return [line for line in raw_lines if isinstance(line, str)]
