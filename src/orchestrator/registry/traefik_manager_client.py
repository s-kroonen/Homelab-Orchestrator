"""Thin async client for Traefik Manager's REST API.

Traefik Manager (https://github.com/chr0nzz/traefik-manager) runs alongside
Traefik on the gateway VM and manages the same ``/etc/traefik/dynamic/``
file-provider directory this repo's Traefik migration already uses (see
``docs/gateway_traefik_migration.md``). It is now the **source of truth**
for proxy routes, and this module is the orchestrator's client of it — the
same relationship ``orchestrator.adapters`` has with Proxmox/IPMI, not an
``orchestrator.adapters`` adapter itself because there's nothing to
dry-run/start/stop here, just short-lived HTTP calls (same reasoning
``orchestrator.registry.traefik_status`` gives for not being an adapter).

This is unlike ``traefik_status.py``, which only ever reads Traefik's own
API and never raises (every failure degrades to ``None``/``UNKNOWN``) — this
module is also used for genuine *writes* (middleware injection, Part 3 of
the Traefik Manager integration), so it raises clear, specific exceptions on
failure instead. Callers decide how to degrade:

* Read paths used for passive drift detection should catch
  :class:`TraefikManagerError` and degrade to "unknown", mirroring
  ``traefik_status``'s silent-degrade convention.
* Explicit admin-triggered writes (Resync, manual middleware sync) should
  let it surface as a real error (503), the same split
  ``docs/proxy_hosts.md`` already documents for the direct-Traefik-API
  Resync path.

Authentication is always ``X-Api-Key`` (generated per-device in Traefik
Manager's own UI: Settings -> Authentication -> API Keys). API-key auth
bypasses Traefik Manager's CSRF checks entirely (see its docs' API
reference), so no CSRF token handling is needed here. Every mutating form
endpoint is called with ``X-Requested-With: fetch`` so Traefik Manager
answers with JSON instead of a 302 redirect to its own UI (see its API
docs' "Response format" section) — without that header these endpoints
assume a browser and never return the JSON body this client expects.
"""

from __future__ import annotations

from typing import Any

import httpx

from orchestrator.config import Settings
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


class TraefikManagerError(Exception):
    """Base class for every failure this client raises."""


class TraefikManagerNotConfigured(TraefikManagerError):
    """``TRAEFIK_MANAGER_URL`` is unset. Distinct from a real failure so a
    caller can tell "not set up" apart from "set up but broken"."""


class TraefikManagerUnavailable(TraefikManagerError):
    """Network error, timeout, or a non-2xx/non-404 response."""


class TraefikManagerNotFound(TraefikManagerError):
    """A 404 from Traefik Manager — e.g. ``route_id`` does not exist."""


def _require_configured(settings: Settings) -> str:
    base_url = settings.traefik_manager_url.strip()
    if not base_url:
        raise TraefikManagerNotConfigured("TRAEFIK_MANAGER_URL is not set")
    return base_url.rstrip("/")


class TraefikManagerClient:
    """One client per call site — cheap, mirrors ``traefik_status``'s
    per-request ``httpx.AsyncClient`` construction rather than a long-lived
    singleton, so settings changes (tests included) take effect immediately."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = _require_configured(settings)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
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

    # -- Routes & middlewares ------------------------------------------------

    async def get_routes(self) -> dict[str, Any]:
        """``GET /api/routes`` — every managed route, middleware and service
        name from every loaded config file. See Traefik Manager's API docs."""
        async with self._client() as client:
            return await self._get_json(client, "/api/routes")

    async def get_route_raw(self, route_id: str) -> dict[str, Any]:
        """``GET /api/routes/{route_id}/raw`` — the route's YAML as stored.

        ``{"raw": "...", "configFile": "...", "proto": "http"}``. Raises
        :class:`TraefikManagerNotFound` if no config file contains that
        router.
        """
        async with self._client() as client:
            return await self._get_json(client, f"/api/routes/{route_id}/raw")

    async def save_route_raw(self, route_id: str, content: str) -> None:
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

    async def save_route(
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

    async def save_middleware(
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

    async def get_configs(self) -> dict[str, Any]:
        """``GET /api/configs`` — every loaded dynamic config file."""
        async with self._client() as client:
            return await self._get_json(client, "/api/configs")
