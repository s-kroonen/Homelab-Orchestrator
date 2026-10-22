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

import contextlib
import io
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from ruamel.yaml import YAML

from orchestrator.adapters.proxy.base import (
    AccessLogTail,
    ConfigFiles,
    EntryPointInfo,
    ProxyAdapter,
    ProxyRouteNotFound,
    RouteInfo,
    RouteOptions,
    SyncEvent,
    find_route,
)
from orchestrator.config import Settings
from orchestrator.logging_setup import get_logger
from orchestrator.registry.config_files import (
    normalize_config_file,
    same_file,
    unknown_file_problem,
)
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
#: installed" as a real DNS name, ever), so no real request can ever match it.
#: It is bound to a REAL entry point read from the live Traefik (never an
#: empty ``entryPoints`` value: Traefik Manager turns that into its own
#: default ``https``, which does not exist on most setups and leaves the
#: router flagged as broken) and sent with TLS disabled, so no certificate is
#: ever requested for the ``.invalid`` name. See docs/proxy_hosts.md.
SHARED_BACKEND_SERVICE_NAME = "orchestrator-maintenance-backend"
_DUMMY_ROUTER_HOSTNAME = "orchestrator-maintenance-backend.invalid"
#: Traefik Manager's ``/save`` names the router after ``serviceName`` but the
#: SERVICE ``<serviceName>-service``. The ``errors`` middleware must point at
#: the service, and a reference to a name that does not exist makes Traefik
#: drop the whole file's routes.
SHARED_BACKEND_SERVICE_REF = f"{SHARED_BACKEND_SERVICE_NAME}-service"

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


#: ``certResolver`` sentinels Traefik Manager's ``/save`` understands: the first
#: keeps the TLS block but names no resolver, the second writes no ``tls``
#: block at all. A blank value is NOT "none" - Traefik Manager substitutes the
#: first configured resolver for it.
CERT_RESOLVER_NONE = "__none__"
CERT_RESOLVER_DISABLED = "__disabled__"


def is_plain_http_entrypoint(name: str, address: str = "") -> bool:
    """``web`` / ``:80`` style entry points serve plain HTTP: a router on one
    must not carry a ``tls`` block (it would stop matching plain requests)."""
    if name.strip().lower() in {"web", "http"}:
        return True
    return address.strip().endswith(":80")


def cert_resolver_form_value(
    cert_resolver: str,
    entry_points: Sequence[str],
    known: Sequence[EntryPointInfo] | None = None,
) -> str:
    """Encode a stored ``cert_resolver`` for Traefik Manager's ``certResolver``
    field. A resolver is sent as its name. No resolver means TLS without one
    (``__none__``) - except when every selected entry point is plain HTTP,
    where no ``tls`` block is wanted at all (``__disabled__``)."""
    resolver = cert_resolver.strip()
    if resolver:
        return resolver
    addresses = {ep.name: ep.address for ep in (known or [])}
    if entry_points and all(
        is_plain_http_entrypoint(name, addresses.get(name, "")) for name in entry_points
    ):
        return CERT_RESOLVER_DISABLED
    return CERT_RESOLVER_NONE


def _router_error_text(raw: Any) -> str | None:
    """Traefik's runtime router ``error`` is a list of strings; flatten it the
    way Traefik Manager's own UI does (``a · b``)."""
    if not raw:
        return None
    items = raw if isinstance(raw, list) else [raw]
    parts = [
        (x if isinstance(x, str) else (x.get("message") if isinstance(x, dict) else str(x)))
        for x in items
    ]
    text = " · ".join(str(p).strip() for p in parts if p and str(p).strip())
    return text or None


def _split_host_port(url: str, *, allow_bare: bool = False) -> tuple[str, str]:
    """Split a ``scheme://host:port`` URL into ``(host, port)`` — shared by
    the maintenance-fallback backend target and the general route CRUD
    below. Returns ``("", "")`` for an unparseable/hostless URL; callers
    decide whether that's acceptable.

    ``allow_bare`` also accepts a scheme-less ``host:port`` / ``10.0.0.5:8000``
    (urlsplit would read the host as a scheme and report no hostname); upstream
    URLs stay strict so a typo like ``not-a-url`` is still rejected."""
    text = url.strip()
    parsed = urlsplit(f"//{text}" if allow_bare and "://" not in text else text)
    host = parsed.hostname or ""
    if not host:
        return "", ""
    port = str(parsed.port) if parsed.port else ("443" if parsed.scheme == "https" else "80")
    return host, port


def _response_detail(response: httpx.Response) -> str:
    """Traefik Manager's own explanation of a refusal (``{"ok": false, "error":
    "..."}``, a 409 asking to confirm a shared change, a missing reference...),
    so it reaches the operator instead of a bare status code."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:300]
    if not isinstance(body, dict):
        return ""
    parts: list[str] = []
    for key in ("error", "message"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    if body.get("needsConfirm"):
        parts.append("it would change definitions shared with other routes (needs confirmation)")
    missing = body.get("missingReferences")
    if isinstance(missing, list) and missing:
        names = ", ".join(
            f"{m.get('kind')} {m.get('name')}" for m in missing if isinstance(m, dict)
        )
        if names:
            parts.append(f"missing: {names}")
    return "; ".join(dict.fromkeys(parts))[:400]


def _split_upstream(url: str) -> tuple[str, str, str]:
    """``scheme://host:port`` -> ``(scheme, host, port)``. Unlike
    :func:`_split_host_port` this keeps the scheme: an ``https://`` upstream
    (Proxmox, PBS) must reach Traefik Manager as ``scheme=https``, or the
    route is written as plain ``http://`` and the backend refuses it."""
    host, port = _split_host_port(url)
    scheme = urlsplit(url).scheme.lower()
    return (scheme if scheme in {"http", "https"} else "http"), host, port


def redirect_route_id(route_ref: str) -> str:
    """The id of the companion plain-HTTP -> HTTPS router for ``route_ref``.
    Derived, so no second reference needs persisting."""
    return f"{route_ref}-redirect"


def _dangling_middlewares(response: Any) -> set[str]:
    """Names of ``errors`` middlewares whose ``service`` is not a service Traefik
    Manager lists (``/api/routes`` -> ``services.http``, plus ``@provider``
    references, which cannot be checked here and are left alone)."""
    if not isinstance(response, dict):
        return set()
    services = response.get("services")
    known = services.get("http") if isinstance(services, dict) else None
    mws = response.get("middlewares")
    if not isinstance(known, list) or not isinstance(mws, list):
        return set()
    found: set[str] = set()
    for mw in mws:
        if not isinstance(mw, dict) or mw.get("type", "http") != "http":
            continue
        name, text = mw.get("name"), mw.get("yaml")
        if not isinstance(name, str) or not isinstance(text, str):
            continue
        try:
            body = _yaml.load(text)
        except Exception:  # unreadable YAML is not ours to judge
            continue
        errors = body.get("errors") if hasattr(body, "get") else None
        target = errors.get("service") if hasattr(errors, "get") else None
        if isinstance(target, str) and "@" not in target and target not in known:
            found.add(name)
    return found


def _routes_to_route_info(apps: Any, dangling: set[str] | None = None) -> list[RouteInfo]:
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
        target = app.get("target")
        upstream = (
            ",".join(s for s in servers if isinstance(s, str))
            if isinstance(servers, list) and servers
            # Traefik Manager writes the string "N/A" when it cannot resolve one.
            else (target if isinstance(target, str) and target != "N/A" else None)
        )
        eps = app.get("entryPoints")
        route_id = app.get("id")
        config_file = app.get("configFile")
        mws = app.get("middlewares")
        infos.append(
            RouteInfo(
                name=name,
                # With several config files `id` is "<file>::<name>" and is the
                # ONLY handle toggle/delete/edit understand; `name` is bare.
                route_id=route_id if isinstance(route_id, str) and route_id else name,
                config_file=config_file if isinstance(config_file, str) else "",
                hostnames=hostnames,
                upstream=upstream or None,
                entry_points=(
                    tuple(e for e in eps if isinstance(e, str)) if isinstance(eps, list) else ()
                ),
                # A disabled route stays in /api/routes (from Traefik Manager's
                # `disabled_routes` store) with "enabled": false.
                enabled=app.get("enabled", True) is not False,
                middlewares=(
                    tuple(m for m in mws if isinstance(m, str)) if isinstance(mws, list) else None
                ),
                dangling_middlewares=(
                    tuple(m for m in mws if isinstance(m, str) and m in (dangling or ()))
                    if isinstance(mws, list)
                    else ()
                ),
            )
        )
    return infos


class TraefikManagerProxyAdapter(ProxyAdapter):
    """One adapter instance per process; each call still builds its own
    short-lived ``httpx.AsyncClient`` (see module docstring) — mirrors
    ``orchestrator.registry.traefik_status``'s per-request client rather
    than a long-lived singleton, so a settings change takes effect
    immediately and no connection pool outlives a single call."""

    HAS_REDIRECT_COMPANION = True

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
            detail = _response_detail(exc.response)
            log.warning("traefik_manager.http_error", url=url, status=status_code, detail=detail)
            raise TraefikManagerUnavailable(
                f"Traefik Manager returned {status_code} for {url}"
                + (f": {detail}" if detail else "")
            ) from exc
        if isinstance(exc, httpx.HTTPError):
            log.warning("traefik_manager.request_failed", url=url, error=str(exc))
            raise TraefikManagerUnavailable(f"could not reach Traefik Manager: {exc}") from exc
        raise exc

    async def _get_json(self, client: httpx.AsyncClient, path: str) -> Any:
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
        entry_points: str,
        cert_resolver: str,
        middlewares: str = "",
        is_edit: bool = False,
        original_id: str = "",
        config_file: str = "",
        scheme: str = "http",
        pass_host_header: bool = True,
        skip_verify: bool = False,
        tls_main: str = "",
        tls_sans: Sequence[str] = (),
        service_ref: str = "",
    ) -> None:
        """``POST /save`` — create or update a route (router + service).

        Only the fields this integration actually needs are exposed; see
        Traefik Manager's API docs for the full field list.

        ``entry_points`` (comma-joined) and ``cert_resolver`` (already encoded,
        see :func:`cert_resolver_form_value`) are required and always posted:
        Traefik Manager's ``/save`` turns an empty ``entryPoints`` into its
        default ``https`` and an empty ``certResolver`` into the first
        configured resolver, neither of which the operator chose.

        ``passHostHeader`` is posted explicitly too: when it is absent Traefik
        Manager writes ``passHostHeader: false``, which makes the proxy send the
        upstream's own address as ``Host`` and bounces browsers to a raw LAN IP
        whenever the backend builds a redirect from it.
        """
        if protocol == "http" and not entry_points.strip():
            raise ValueError("entry_points must not be empty (Traefik Manager would default it)")
        if not cert_resolver.strip():
            raise ValueError("cert_resolver must be a name or a sentinel, never blank")
        data: dict[str, str] = {
            "serviceName": service_name,
            "protocol": protocol,
            "targetIp": target_ip,
            "targetPort": target_port,
            "subdomain": subdomain,
            "httpRule": http_rule,
            "entryPoints": entry_points,
            "certResolver": cert_resolver,
            "middlewares": middlewares,
            "isEdit": "true" if is_edit else "false",
            "originalId": original_id,
            "configFile": config_file,
            "scheme": scheme,
            "passHostHeader": "true" if pass_host_header else "false",
        }
        if skip_verify:
            data["insecureSkipVerify"] = "true"
        if tls_main:
            data["tlsWildcardMain"] = tls_main
            if tls_sans:
                data["tlsWildcardSans"] = "\n".join(tls_sans)
        if service_ref:
            data["serviceRef"] = service_ref
        async with self._client() as client:
            await self._post_json(client, "/save", data=data, headers={"X-Requested-With": "fetch"})

    async def _delete_route(self, route_id: str, config_file: str = "") -> None:
        """``POST /delete/{route_id}`` — delete a route (router + its
        service, unless another router still references it). Form-
        urlencoded, per Traefik Manager's API docs. Raises
        :class:`TraefikManagerNotFound` (via ``_raise_for``) on a 404 —
        "no config file and no disabled-route record holds that router".

        Traefik Manager strips a ``<file>::`` prefix to the bare router name and
        then searches EVERY config file for it unless the form names one
        (``configFile``) - so with several files the route's own file is sent
        along, or a same-named router in another file could be removed."""
        data = {"configFile": config_file} if config_file else {}
        async with self._client() as client:
            await self._post_json(
                client,
                f"/delete/{quote(route_id, safe=':')}",
                data=data,
                headers={"X-Requested-With": "fetch"},
            )

    async def _toggle_route(self, route_id: str, enable: bool) -> None:
        """``POST /api/routes/{route_id}/toggle`` with ``{"enable": bool}``.

        Disabling moves the router (and its service, unless another router
        shares it) out of the config file into Traefik Manager's own
        ``disabled_routes`` settings store; enabling writes them back. Traefik
        stops/starts serving it on its next file-provider reload. JSON body.

        TRAP: ``route_id`` must be the id ``GET /api/routes`` lists
        (``<file>::<name>`` with several config files). Toggling an unknown id
        - a bare name, say - is a silent no-op that still answers 200, and
        enabling only finds a route whose id is the key of its disabled-routes
        record. Callers resolve the id first and confirm the outcome after
        (:meth:`_toggle_confirmed`)."""
        async with self._client() as client:
            await self._post_json(
                client,
                f"/api/routes/{quote(route_id, safe=':')}/toggle",
                json={"enable": bool(enable)},
                headers={"X-Requested-With": "fetch"},
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
        return _routes_to_route_info(apps, _dangling_middlewares(response))

    # -- ProxyAdapter: maintenance-fallback write path ----------------------

    def _public_target(self) -> tuple[str, str]:
        """Split ``ORCHESTRATOR_PUBLIC_URL`` into ``(host, port)`` for
        ``/save``'s ``targetIp``/``targetPort`` fields."""
        return _split_host_port(self._settings.orchestrator_public_url, allow_bare=True)

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

    async def _pick_internal_entrypoint(self) -> str | None:
        """A real, existing entry point for the never-matched maintenance
        backend router: the configured default, else ``websecure``, else the
        first non-``traefik`` one. ``None`` when the live list is unreadable."""
        from orchestrator.registry.proxy_validation import resolve_default_entry_points

        live = await self.list_entrypoints()
        if not live:
            return None
        picked = resolve_default_entry_points(self._settings.default_entry_points_list, live)
        return picked[0] if picked else None

    async def _ensure_shared_backend_service(self) -> SyncEvent | None:
        """Create :data:`SHARED_BACKEND_SERVICE_NAME` if it doesn't already
        exist. Idempotent: checks ``GET /api/routes``'s ``services.http``
        list first and makes no write at all when it's already there."""
        routes = await self._get_routes()
        services = routes.get("services") if isinstance(routes, dict) else None
        http_services = services.get("http") if isinstance(services, dict) else None
        if isinstance(http_services, list) and SHARED_BACKEND_SERVICE_REF in http_services:
            return None

        entry_point = await self._pick_internal_entrypoint()
        if entry_point is None:
            # Never guess an entry point: a router bound to one that does
            # not exist shows up broken in Traefik Manager. Abort the whole
            # fallback (the caller logs and fails open) rather than create a
            # middleware that points at a service that does not exist.
            raise TraefikManagerUnavailable(
                "no usable entry point for the maintenance backend router "
                "(Traefik's entry points could not be read)"
            )

        host, port = self._public_target()
        if not host:
            raise TraefikManagerUnavailable(
                "ORCHESTRATOR_PUBLIC_URL "
                f"({self._settings.orchestrator_public_url!r}) has no host in it - "
                "set it to this orchestrator's address as the gateway reaches it, "
                "e.g. http://10.0.0.5:8000"
            )
        await self._save_route(
            service_name=SHARED_BACKEND_SERVICE_NAME,
            protocol="http",
            target_ip=host,
            target_port=port,
            http_rule=f"Host(`{_DUMMY_ROUTER_HOSTNAME}`)",
            entry_points=entry_point,
            cert_resolver=CERT_RESOLVER_DISABLED,
            pass_host_header=False,
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

        content = self._middleware_yaml(SHARED_BACKEND_SERVICE_REF, slug)
        await self._save_middleware(name, content, protocol="http", is_edit=existing is not None)
        return SyncEvent(
            "middleware.updated" if existing is not None else "middleware.created",
            name,
            {"query": f"/maintenance/{slug}", "service": SHARED_BACKEND_SERVICE_REF},
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
        # Never trust the write: read the route back and confirm the middleware
        # is really on it.
        after = await self._get_route_raw(route_id)
        after_block = self._router_block(
            _yaml.load(after.get("raw", "") if isinstance(after, dict) else "")
        )
        after_mws = list(after_block[1].get("middlewares") or []) if after_block else []
        if middleware_name not in after_mws:
            raise TraefikManagerUnavailable(
                f"Traefik Manager accepted the write but {middleware_name} is not on "
                f"route {route_id} afterwards"
            )
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

    def maintenance_middleware_name(self, service_slug: str) -> str | None:
        return maintenance_middleware_name(service_slug)

    async def ensure_maintenance_fallback(
        self, *, hostname: str, service_slug: str, raise_errors: bool = False
    ) -> list[SyncEvent]:
        if not self._settings.traefik_manager_url.strip() or (
            not self._settings.orchestrator_public_url.strip()
        ):
            if raise_errors:
                raise TraefikManagerUnavailable(
                    "the maintenance page needs TRAEFIK_MANAGER_URL and ORCHESTRATOR_PUBLIC_URL"
                )
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
            if raise_errors:
                raise
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
    def _target_or_raise(upstream: str) -> tuple[str, str, str]:
        scheme, host, port = _split_upstream(upstream)
        if not host:
            raise ValueError(
                f"upstream {upstream!r} is not a valid URL with a host "
                "(expected e.g. http://10.0.0.5:8080)"
            )
        return scheme, host, port

    async def _route_fields(
        self, entry_points: Sequence[str], cert_resolver: str
    ) -> tuple[str, str]:
        """``(entryPoints, certResolver)`` form values. Raises ``ValueError``
        on an empty selection - nothing is ever posted for the manager to
        default."""
        eps = [e.strip() for e in entry_points if e and e.strip()]
        if not eps:
            raise ValueError("at least one entry point is required")
        known = await self.list_entrypoints() if not cert_resolver.strip() else None
        return ",".join(eps), cert_resolver_form_value(cert_resolver, eps, known)

    async def _plain_http_entrypoints(self) -> list[str]:
        """Entry points a plain-HTTP -> HTTPS redirect router should listen on
        (``web`` / ``:80``). Never the internal ``traefik`` one."""
        known = await self.list_entrypoints()
        if known is None:
            return ["web"]
        found = [
            ep.name
            for ep in known
            if ep.name != "traefik" and is_plain_http_entrypoint(ep.name, ep.address)
        ]
        if not found:
            raise ValueError(
                "cannot add an HTTP -> HTTPS redirect: Traefik has no plain-HTTP "
                "entry point (web / :80) to redirect from"
            )
        return found

    async def _read_routes(self) -> list[RouteInfo]:
        """Every HTTP route Traefik Manager lists, with its real ``id``. Raises
        (unlike :meth:`list_routes`) - a write that cannot see what it is
        about to change must not guess."""
        response = await self._get_routes()
        apps = response.get("apps") if isinstance(response, dict) else None
        return _routes_to_route_info(apps)

    async def _save_redirect_route(
        self,
        route_ref: str,
        *,
        hostname: str,
        existing: RouteInfo | None,
        config_file: str = "",
        move_to: str = "",
    ) -> None:
        """The companion router on the plain-HTTP entry point that redirects to
        HTTPS and shares the main route's service - the pair every hand-written
        ``<name>-redirect`` / ``<name>`` setup has. ``existing`` is the
        companion Traefik Manager already lists (edit it by its real id and
        file), or ``None`` to create it (next to the main route, in
        ``config_file``). ``move_to`` re-saves ``existing`` into that file
        (Traefik Manager removes it from the file named in the ``<file>::``
        prefix of ``originalId``)."""
        rid = redirect_route_id(route_ref)
        original_id = existing.ref if existing is not None else ""
        target_file = existing.config_file if existing is not None else config_file
        if existing is not None and move_to:
            original_id = (
                f"{existing.config_file}::{existing.name}" if existing.config_file else existing.ref
            )
            target_file = move_to
        await self._save_route(
            service_name=rid,
            protocol="http",
            http_rule=f"Host(`{hostname}`)",
            entry_points=",".join(await self._plain_http_entrypoints()),
            cert_resolver=CERT_RESOLVER_DISABLED,
            middlewares=self._settings.traefik_redirect_middleware.strip() or "redirect-to-https",
            service_ref=f"{route_ref}-service",
            is_edit=existing is not None,
            original_id=original_id,
            config_file=target_file,
        )

    async def _sync_redirect_route(
        self,
        route_ref: str,
        *,
        hostname: str,
        want: bool,
        previous: RouteOptions | None,
        routes: Sequence[RouteInfo] | None = None,
        main: RouteInfo | None = None,
    ) -> None:
        if routes is None:
            routes = await self._read_routes()
        rid = redirect_route_id(route_ref)
        # The companion normally lives next to the main route (same config file).
        siblings = [r for r in routes if rid in (r.route_id, r.name)]
        beside = [r for r in siblings if main is not None and r.config_file == main.config_file]
        existing = (beside or siblings or [None])[0]
        if want:
            try:
                await self._save_redirect_route(
                    route_ref,
                    hostname=hostname,
                    existing=existing,
                    config_file=main.config_file if main is not None else "",
                )
            except TraefikManagerNotFound:
                await self._save_redirect_route(
                    route_ref,
                    hostname=hostname,
                    existing=None,
                    config_file=main.config_file if main is not None else "",
                )
            return
        # Only take a redirect router away when there may be one: the host
        # had it enabled before (or that is unknown) - never a blind delete on
        # every edit.
        if (previous is None or previous.http_redirect) and existing is not None:
            with contextlib.suppress(TraefikManagerNotFound):
                await self._delete_route(existing.ref, existing.config_file)

    async def _toggle_confirmed(self, route: RouteInfo, enabled: bool) -> None:
        """Toggle ``route`` by its real id, then READ IT BACK.

        Traefik Manager answers 200 to a toggle it ignored (unknown id, a
        disabled-routes record under another key, ...), so a success response
        proves nothing. The route must be listed with ``enabled`` as asked, or
        this raises."""
        await self._toggle_route(route.ref, enabled)
        after = find_route(await self._read_routes(), route.ref)
        if after is None or after.enabled != enabled:
            now = "missing" if after is None else ("enabled" if after.enabled else "disabled")
            raise TraefikManagerUnavailable(
                "Traefik Manager accepted the request but the route is still "
                f"{now}: {route.ref}"
                + (
                    " (it is no longer listed)"
                    if after is None
                    else " (it did not change state - check the id and Traefik Manager's logs)"
                )
            )

    def maintenance_fallback_available(self) -> bool:
        return bool(
            self._settings.traefik_manager_url.strip()
            and self._settings.orchestrator_public_url.strip()
        )

    def route_ref_for(self, hostname: str) -> str:
        return route_name_for_hostname(hostname)

    def owns_route_ref(self, route_ref: str) -> bool:
        # Everything this adapter creates is prefixed (see
        # route_name_for_hostname); anything else was adopted from a
        # hand-written setup and is never overwritten or deleted.
        return route_ref.startswith("orchestrator-")

    async def create_route(
        self,
        *,
        hostname: str,
        upstream: str,
        entry_points: Sequence[str],
        cert_resolver: str = "",
        options: RouteOptions | None = None,
        config_file: str = "",
    ) -> str:
        opts = options or RouteOptions()
        scheme, host, port = self._target_or_raise(upstream)
        eps, resolver = await self._route_fields(entry_points, cert_resolver)
        name = route_name_for_hostname(hostname)
        cfile = await self._checked_config_file(config_file)
        await self._save_route(
            service_name=name,
            protocol="http",
            target_ip=host,
            target_port=port,
            http_rule=f"Host(`{hostname}`)",
            entry_points=eps,
            cert_resolver=resolver,
            is_edit=False,
            config_file=cfile,
            scheme=scheme,
            pass_host_header=opts.pass_host_header,
            skip_verify=opts.skip_upstream_tls_verify,
            tls_main=opts.tls_main,
            tls_sans=opts.tls_sans,
        )
        try:
            if opts.http_redirect:
                # The companion goes into the SAME file as its main route.
                await self._save_redirect_route(
                    name, hostname=hostname, existing=None, config_file=cfile
                )
            if cfile:
                await self._confirm_created_in(name, hostname, cfile)
        except Exception:
            # Never leave a half-made pair (or a route in the wrong file) behind:
            # the caller treats this create as failed and keeps no record of it.
            try:
                await self._delete_route(name, cfile)
            except Exception as exc:  # pragma: no cover - best effort
                log.warning("traefik_manager.create_rollback_failed", error=str(exc))
            raise
        return name

    async def _checked_config_file(self, config_file: str) -> str:
        """The config file name to post, or ``ValueError`` when Traefik Manager
        would NOT write to it (a fixed ``CONFIG_PATHS`` without it): its ``/save``
        then quietly uses its default file - never let that happen unnoticed."""
        cfile = normalize_config_file(config_file)
        if not cfile:
            return ""
        problem = unknown_file_problem(cfile, await self.list_config_files())
        if problem:
            raise ValueError(problem)
        return cfile

    async def _confirm_created_in(self, name: str, hostname: str, cfile: str) -> None:
        """Read the new route back and make sure it is in ``cfile``. A manager
        that reports no config files (single fixed file) says ``""`` - nothing
        to compare then."""
        routes = await self._read_routes()
        here = [r for r in routes if r.name == name and hostname in r.hostnames]
        if here and all(r.config_file and not same_file(r.config_file, cfile) for r in here):
            raise TraefikManagerUnavailable(
                f"Traefik Manager saved the new route {name} into {here[0].config_file} instead "
                f"of {cfile} (it only writes to the files it knows) - it was removed again."
            )

    async def list_config_files(self) -> ConfigFiles | None:
        """``GET /api/configs`` - ``{"files": [{"label", "path"}], "configDirSet"}``.
        With ``CONFIG_DIR`` set Traefik Manager creates an unknown ``<name>.yml``
        in it; with only ``CONFIG_PATHS`` it uses its DEFAULT file for a name it
        does not know. Older versions without the endpoint are read from the
        ``configFile`` of ``GET /api/routes`` (``can_create`` then unknown)."""
        if not self._settings.traefik_manager_url.strip():
            return None
        response: Any = None
        try:
            async with self._client() as client:
                response = await self._get_json(client, "/api/configs")
        except TraefikManagerNotConfigured:
            return None
        except TraefikManagerError as exc:
            log.info("traefik_manager.configs_unavailable", error=str(exc))
        files_raw = response.get("files") if isinstance(response, dict) else None
        if isinstance(files_raw, list):
            names: list[str] = []
            for item in files_raw:
                label = item.get("label") if isinstance(item, dict) else item
                if isinstance(label, str) and label.strip() and label.strip() not in names:
                    names.append(label.strip())
            return ConfigFiles(
                files=tuple(names),
                can_create=bool(response.get("configDirSet")),
                default=names[0] if names else "",
            )
        routes = await self.list_routes()
        if routes is None:
            return None
        seen = sorted({r.config_file for r in routes if r.config_file})
        return ConfigFiles(files=tuple(seen), can_create=None)

    async def move_route(
        self,
        route_ref: str,
        *,
        hostname: str,
        upstream: str,
        entry_points: Sequence[str],
        cert_resolver: str = "",
        options: RouteOptions | None = None,
        config_file: str,
    ) -> str:
        """Move a route (and its redirect companion) into ``config_file``.

        Traefik Manager's ``/save`` moves a route when ``originalId`` carries the
        ``<old file>::<name>`` prefix and ``configFile`` names another file: it
        removes the router (and its service, unless another router shares it)
        from the old file and writes both into the new one. The prefix is built
        here even when Traefik Manager lists bare ids (a ``CONFIG_DIR`` with one
        file), or it would treat the save as an edit in the NEW file and leave
        the old copy behind.

        The companion is moved FIRST: it references the main route's service by
        name, and while it still sits in the old file that service counts as
        shared and would not be removed from there. Everything is read back
        afterwards - exactly one copy of each router, in ``config_file``."""
        target = await self._checked_config_file(config_file)
        if not target:
            raise ValueError("a target config file is required to move a route")
        opts = options or RouteOptions()
        scheme, host, port = self._target_or_raise(upstream)
        eps, resolver = await self._route_fields(entry_points, cert_resolver)
        routes = await self._read_routes()
        main = find_route(routes, route_ref, hostname)
        if main is None:
            raise ProxyRouteNotFound(f"{route_ref}: not found")
        if same_file(main.config_file, target):
            return main.config_file
        if not main.config_file:
            raise TraefikManagerUnavailable(
                "Traefik Manager does not report which config file this route is in "
                "(it has a single config file), so there is nothing to move it between."
            )
        if not main.enabled:
            raise TraefikManagerUnavailable(
                "the route is switched off in Traefik Manager - it is parked outside its config "
                "file there. Enable it (push), move it, then disable it again."
            )
        rid = redirect_route_id(main.name)
        siblings = [r for r in routes if rid in (r.route_id, r.name)]
        beside = [r for r in siblings if r.config_file == main.config_file]
        companion = (beside or [None])[0]
        if companion is not None and not companion.enabled:
            raise TraefikManagerUnavailable(
                "the HTTP -> HTTPS redirect router is switched off in Traefik Manager - enable it "
                "first (push), then move the route."
            )
        old_file = main.config_file
        if companion is not None:
            await self._save_redirect_route(
                main.name, hostname=hostname, existing=companion, move_to=target
            )
        try:
            await self._save_route(
                service_name=main.name,
                protocol="http",
                target_ip=host,
                target_port=port,
                http_rule=f"Host(`{hostname}`)",
                entry_points=eps,
                cert_resolver=resolver,
                is_edit=True,
                original_id=f"{old_file}::{main.name}",
                config_file=target,
                scheme=scheme,
                pass_host_header=opts.pass_host_header,
                skip_verify=opts.skip_upstream_tls_verify,
                tls_main=opts.tls_main,
                tls_sans=opts.tls_sans,
            )
        except TraefikManagerNotFound as exc:
            raise ProxyRouteNotFound(str(exc)) from exc
        # Read it back: Traefik Manager answers 200 even when it wrote elsewhere.
        after = await self._read_routes()
        for name in (main.name, *([rid] if companion is not None else [])):
            copies = [r for r in after if r.name == name and hostname in r.hostnames]
            if len(copies) != 1 or not same_file(copies[0].config_file, target):
                where = ", ".join(r.config_file or "(default)" for r in copies) or "nowhere"
                raise TraefikManagerUnavailable(
                    f"Traefik Manager accepted the move but {name} is now in {where}, expected "
                    f"only {target} (it was in {old_file}) - check both files in Traefik Manager."
                )
        return target

    async def update_route(
        self,
        route_ref: str,
        *,
        hostname: str,
        upstream: str,
        entry_points: Sequence[str],
        cert_resolver: str = "",
        options: RouteOptions | None = None,
        previous_options: RouteOptions | None = None,
    ) -> None:
        opts = options or RouteOptions()
        scheme, host, port = self._target_or_raise(upstream)
        eps, resolver = await self._route_fields(entry_points, cert_resolver)
        # Edit the route by the id Traefik Manager lists and in the config file
        # it lives in: with several files `/save` otherwise looks for it in the
        # DEFAULT file, does not find it there and writes a second copy.
        routes = await self._read_routes()
        main = find_route(routes, route_ref, hostname)
        if main is None:
            raise ProxyRouteNotFound(f"{route_ref}: not found")
        try:
            await self._save_route(
                service_name=main.name,
                protocol="http",
                target_ip=host,
                target_port=port,
                http_rule=f"Host(`{hostname}`)",
                entry_points=eps,
                cert_resolver=resolver,
                is_edit=True,
                original_id=main.ref,
                config_file=main.config_file,
                scheme=scheme,
                pass_host_header=opts.pass_host_header,
                skip_verify=opts.skip_upstream_tls_verify,
                tls_main=opts.tls_main,
                tls_sans=opts.tls_sans,
            )
        except TraefikManagerNotFound as exc:
            raise ProxyRouteNotFound(str(exc)) from exc
        await self._sync_redirect_route(
            route_ref,
            hostname=hostname,
            want=opts.http_redirect,
            previous=previous_options,
            routes=routes,
            main=main,
        )

    async def delete_route(
        self, route_ref: str, *, with_redirect: bool = True, hostname: str | None = None
    ) -> None:
        routes = await self._read_routes()
        main = find_route(routes, route_ref, hostname)
        # The companion redirect router first (it shares the main route's
        # service), then the route itself.
        if with_redirect:
            rid = redirect_route_id(route_ref)
            siblings = [r for r in routes if rid in (r.route_id, r.name)]
            beside = [r for r in siblings if main is not None and r.config_file == main.config_file]
            companion = (beside or siblings or [None])[0]
            if companion is not None:
                with contextlib.suppress(TraefikManagerNotFound):
                    await self._delete_route(companion.ref, companion.config_file)
        if main is None:
            raise ProxyRouteNotFound(f"{route_ref}: not found")
        try:
            await self._delete_route(main.ref, main.config_file)
        except TraefikManagerNotFound as exc:
            raise ProxyRouteNotFound(str(exc)) from exc

    async def set_route_enabled(
        self,
        route_ref: str,
        enabled: bool,
        *,
        with_redirect: bool = False,
        hostname: str | None = None,
    ) -> None:
        """Toggle the route (and its ``-redirect`` companion) on Traefik
        Manager. ``route_ref`` may be the bare name or the full id: it is
        resolved against ``GET /api/routes`` first, because Traefik Manager
        only acts on the id it lists (``<file>::<name>`` with several config
        files) and answers 200 to anything else. Every toggle is read back.

        Ordered so a half-done toggle never leaves a plain-HTTP redirect
        pointing at a route that is gone: disable takes the redirect away
        first, enable brings the main route back first. If the second call
        fails the first is undone (best effort) and the error raised."""
        routes = await self._read_routes()
        main = find_route(routes, route_ref, hostname)
        if main is None:
            raise ProxyRouteNotFound(
                f"Traefik Manager has no route {route_ref!r} to "
                f"{'enable' if enabled else 'disable'}"
            )
        if not with_redirect:
            await self._toggle_confirmed(main, enabled)
            return
        rid = redirect_route_id(route_ref)
        siblings = [r for r in routes if rid in (r.route_id, r.name)]
        beside = [r for r in siblings if r.config_file == main.config_file]
        companion = (beside or siblings or [None])[0]
        if companion is None:
            # No companion router exists (never created, or removed by hand):
            # nothing to take along.
            await self._toggle_confirmed(main, enabled)
            return
        pair = [companion, main]
        if enabled:
            pair.reverse()
        first, second = pair
        await self._toggle_confirmed(first, enabled)
        try:
            await self._toggle_confirmed(second, enabled)
        except Exception:
            try:
                await self._toggle_confirmed(first, not enabled)
            except Exception as exc:  # pragma: no cover - best effort
                log.warning(
                    "traefik_manager.toggle_rollback_failed", route=first.ref, error=str(exc)
                )
            raise

    async def list_domains(self) -> list[str] | None:
        if not self._settings.traefik_manager_url.strip():
            return None
        try:
            async with self._client() as client:
                response = await self._get_json(client, "/api/settings")
        except TraefikManagerError as exc:
            log.warning("traefik_manager.settings_failed", error=str(exc))
            return None
        raw = response.get("domains") if isinstance(response, dict) else None
        parts = raw if isinstance(raw, list) else str(raw or "").split(",")
        return [str(d).strip() for d in parts if str(d).strip()]

    async def list_middlewares(self) -> list[str] | None:
        if not self._settings.traefik_manager_url.strip():
            return None
        try:
            async with self._client() as client:
                response = await self._get_json(client, "/api/traefik/middlewares")
        except TraefikManagerError as exc:
            log.warning("traefik_manager.middlewares_failed", error=str(exc))
            return None
        http = response.get("http") if isinstance(response, dict) else None
        if not isinstance(http, list):
            return None
        return [m["name"] for m in http if isinstance(m, dict) and isinstance(m.get("name"), str)]

    # -- ProxyAdapter: access-log read path (GeoIP traffic visibility) ------
    #
    # ``GET /api/traefik/logs?lines=N`` (max 1000 per Traefik Manager's own
    # API docs) — Traefik Manager already tails Traefik's own access log for
    # its own Logs tab; this reuses that exact read rather than this app
    # gaining any new SSH/filesystem access of its own. Requires an access
    # log path configured on the gateway (``ACCESS_LOG_PATH`` or Traefik
    # Manager's Settings). Without one — or when the path is refused —
    # Traefik Manager still answers 200 but with ``{"error": "...",
    # "lines": []}``; :meth:`fetch_access_log` surfaces that ``error`` text
    # verbatim so it never reads as "no traffic".

    async def fetch_access_log(self, lines: int = 200) -> AccessLogTail:
        if not self._settings.traefik_manager_url.strip():
            return AccessLogTail(lines=None, error="TRAEFIK_MANAGER_URL is not set.")
        n = min(max(1, int(lines)), 1000)
        try:
            async with self._client() as client:
                response = await self._get_json(client, f"/api/traefik/logs?lines={n}")
        except TraefikManagerNotConfigured as exc:
            return AccessLogTail(lines=None, error=str(exc))
        except TraefikManagerError as exc:
            log.warning("traefik_manager.tail_access_log_failed", error=str(exc))
            return AccessLogTail(lines=None, error=str(exc))
        if not isinstance(response, dict):
            return AccessLogTail(lines=None, error="Traefik Manager returned an unexpected body.")
        error = response.get("error")
        raw_lines = response.get("lines")
        clean = (
            [line for line in raw_lines if isinstance(line, str)]
            if isinstance(raw_lines, list)
            else []
        )
        if isinstance(error, str) and error.strip():
            log.warning("traefik_manager.access_log_error", error=error)
            return AccessLogTail(lines=clean or None, error=f"Traefik Manager: {error.strip()}")
        return AccessLogTail(lines=clean)

    async def tail_access_log(self, lines: int = 200) -> list[str] | None:
        tail = await self.fetch_access_log(lines=lines)
        if tail.error is not None:
            return None
        return tail.lines

    # -- ProxyAdapter: runtime routers (access-log attribution) -------------
    #
    # ``GET /api/traefik/routers`` — Traefik Manager proxies Traefik's own
    # runtime ``/api/{http,tcp,udp}/routers`` (shape ``{"http": [...],
    # "tcp": [...], "udp": [...], "reachable": bool, "complete": bool}``).
    # Unlike ``/api/routes`` (only the file-provider routes Traefik Manager
    # manages, bare names), this covers EVERY provider and uses the exact
    # ``name@provider`` the access log writes — so it is the primary
    # router-name -> hostname source for the access-log collector, without
    # this app needing TRAEFIK_API_URL itself.

    async def list_runtime_routers(self) -> list[RouteInfo] | None:
        if not self._settings.traefik_manager_url.strip():
            return None
        try:
            async with self._client() as client:
                response = await self._get_json(client, "/api/traefik/routers")
        except TraefikManagerError as exc:
            log.warning("traefik_manager.runtime_routers_failed", error=str(exc))
            return None
        if not isinstance(response, dict):
            return None
        if response.get("reachable") is False:
            log.warning("traefik_manager.runtime_routers_unreachable")
            return None
        routers = response.get("http")
        if not isinstance(routers, list):
            return []
        infos: list[RouteInfo] = []
        for router in routers:
            if not isinstance(router, dict):
                continue
            name = router.get("name")
            rule = router.get("rule")
            if not isinstance(name, str) or not isinstance(rule, str):
                continue
            hostnames = extract_hostnames(rule)
            if not hostnames:
                continue
            service = router.get("service")
            eps = router.get("entryPoints")
            tls = router.get("tls")
            resolver = tls.get("certResolver") if isinstance(tls, dict) else None
            status = router.get("status")
            infos.append(
                RouteInfo(
                    name=name,
                    hostnames=hostnames,
                    upstream=service if isinstance(service, str) else None,
                    entry_points=(
                        tuple(e for e in eps if isinstance(e, str)) if isinstance(eps, list) else ()
                    ),
                    status=status if isinstance(status, str) else None,
                    error=_router_error_text(router.get("error")),
                    cert_resolver=resolver if isinstance(resolver, str) else None,
                )
            )
        return infos

    # -- ProxyAdapter: entry points / cert resolvers -------------------------
    #
    # ``GET /api/traefik/entrypoints`` - Traefik Manager proxies Traefik's own
    # ``/api/entrypoints`` (a JSON list of ``{"name": "web", "address": ":80",
    # ...}``), or answers 502 ``{"error": "Traefik API unreachable"}``.
    # Certificate resolvers have no endpoint of their own: they are the union of
    # ``GET /api/settings`` (``cert_resolver``, comma-separated), the static
    # config (``GET /api/static/config`` -> ``parsed.certificatesResolvers``,
    # only when Traefik Manager has the static config path set) and the
    # resolvers the runtime routers use - see :meth:`list_cert_resolver_sources`.

    async def list_entrypoints(self) -> list[EntryPointInfo] | None:
        if not self._settings.traefik_manager_url.strip():
            return None
        try:
            async with self._client() as client:
                response = await self._get_json(client, "/api/traefik/entrypoints")
        except TraefikManagerError as exc:  # includes the 502 "API unreachable"
            log.warning("traefik_manager.entrypoints_failed", error=str(exc))
            return None
        if not isinstance(response, list):
            return None
        found: list[EntryPointInfo] = []
        for item in response:
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]:
                address = item.get("address")
                found.append(
                    EntryPointInfo(
                        name=item["name"], address=address if isinstance(address, str) else ""
                    )
                )
        return found

    async def _resolvers_from_settings(self) -> list[str] | None:
        """(a) ``GET /api/settings`` -> ``cert_resolver``, Traefik Manager's own
        comma list. ``None`` = not readable."""
        try:
            async with self._client() as client:
                response = await self._get_json(client, "/api/settings")
        except TraefikManagerError as exc:
            log.warning("traefik_manager.settings_failed", error=str(exc))
            return None
        raw = response.get("cert_resolver") if isinstance(response, dict) else None
        if not isinstance(raw, str):
            return None
        return [
            part.strip()
            for part in raw.split(",")
            if part.strip() and part.strip().lower() != "none"
        ]

    async def _resolvers_from_static_config(self) -> list[str] | None:
        """(b) ``certificatesResolvers`` of Traefik's static configuration.

        ``GET /api/static/config`` answers ``{"raw", "parsed", "path"}`` when
        Traefik Manager has ``STATIC_CONFIG_PATH`` (or Settings -> static config
        path) configured and the file is mounted into ITS container; otherwise
        404 ``{"error": ...}`` (500 when the file is not valid YAML). Any of
        those is "not available", never an error. Needs only the same API key as
        every other read."""
        try:
            async with self._client() as client:
                response = await self._get_json(client, "/api/static/config")
        except TraefikManagerError as exc:
            log.info("traefik_manager.static_config_unavailable", error=str(exc))
            return None
        parsed = response.get("parsed") if isinstance(response, dict) else None
        if not isinstance(parsed, dict):
            return None
        resolvers = parsed.get("certificatesResolvers")
        if not isinstance(resolvers, dict):
            return []
        return [str(k).strip() for k in resolvers if str(k).strip()]

    async def list_cert_resolver_sources(self) -> dict[str, list[str]] | None:
        """The union of every place a certificate resolver can be seen, each name
        tagged with the sources that report it (stable order: Traefik Manager's
        setting, Traefik's static config, resolvers routers really use, then
        ``TRAEFIK_DEFAULT_CERT_RESOLVER``):

        * ``tm_settings`` - ``/api/settings`` ``cert_resolver``;
        * ``static_config`` - ``/api/static/config`` ``certificatesResolvers``;
        * ``runtime_router`` - ``tls.certResolver`` of a router Traefik serves;
        * ``default_setting`` - the orchestrator's own default.

        ``None`` when none of the three live sources could be read (the
        configured default alone proves nothing about what exists) - the caller
        then offers free text. A source that cannot be read just adds nothing."""
        if not self._settings.traefik_manager_url.strip():
            return None
        from_settings = await self._resolvers_from_settings()
        from_static = await self._resolvers_from_static_config()
        runtime = await self.list_runtime_routers()
        from_runtime = (
            None if runtime is None else [r.cert_resolver for r in runtime if r.cert_resolver]
        )
        if from_settings is None and from_static is None and from_runtime is None:
            return None
        out: dict[str, list[str]] = {}
        for tag, names in (
            ("tm_settings", from_settings),
            ("static_config", from_static),
            ("runtime_router", from_runtime),
            ("default_setting", [self._settings.traefik_default_cert_resolver]),
        ):
            for raw in names or []:
                name = str(raw).strip()
                if not name or name.lower() == "none":
                    continue
                tags = out.setdefault(name, [])
                if tag not in tags:
                    tags.append(tag)
        return out
