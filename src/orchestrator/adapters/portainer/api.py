"""Live Portainer adapter: read-only, through the HTTP API.

Authenticates with an access token (``X-API-Key``). Container data comes through
Portainer's Docker proxy (``/api/endpoints/{id}/docker/...``), so one key reaches every
environment Portainer manages.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from typing import Any

import httpx

from orchestrator.adapters.errors import (
    AdapterAuthError,
    AdapterNotConfigured,
    AdapterRequestError,
    AdapterTlsError,
    AdapterUnreachable,
)
from orchestrator.adapters.portainer import parse
from orchestrator.adapters.portainer.base import (
    Container,
    ContainerDetail,
    ContainerStats,
    Environment,
    LogLine,
    PortainerAdapter,
    PortainerCheck,
    Stack,
    StackListing,
)
from orchestrator.config import Settings
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

#: Repeated reads inside this window (a page load asks for the same list several times)
#: are served from memory. Logs and stats are never cached.
CACHE_TTL_S = 5.0
_MAX_TAIL = 2000


def _is_tls_failure(exc: BaseException) -> bool:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(cause):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


class LivePortainerAdapter(PortainerAdapter):
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._url = settings.portainer_url.strip().rstrip("/")
        self._public = settings.portainer_public_url.strip().rstrip("/") or self._url
        self._key = settings.portainer_api_key.get_secret_value()
        self._verify = settings.portainer_verify_tls
        self._timeout = float(settings.controller_timeout_s)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, Any]] = {}
        if self._url and not self._verify:
            log.warning("adapter.tls_verification_disabled", flavor="portainer", host=self._url)

    # -- plumbing ----------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self._url and self._key)

    @property
    def base_url(self) -> str:
        return self._public

    async def start(self) -> None:
        if self._client is None and self.configured:
            self._client = httpx.AsyncClient(
                base_url=self._url,
                headers={"X-API-Key": self._key, "Accept": "application/json"},
                verify=self._verify,
                timeout=self._timeout,
                transport=self._transport,
            )

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        if not self.configured:
            raise AdapterNotConfigured(
                "Portainer is not configured - set PORTAINER_URL and PORTAINER_API_KEY"
            )
        if self._client is None:
            await self.start()
        assert self._client is not None
        try:
            response = await self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise AdapterUnreachable(f"Portainer timed out on GET {path}: {exc}") from exc
        except httpx.TransportError as exc:
            if _is_tls_failure(exc):
                raise AdapterTlsError(
                    f"Portainer's TLS certificate was rejected on GET {path}: {exc}. The host "
                    "answered - this is certificate trust. Set PORTAINER_VERIFY_TLS=false or "
                    "trust its CA."
                ) from exc
            raise AdapterUnreachable(f"Portainer unreachable on GET {path}: {exc}") from exc
        if response.status_code in (401, 403):
            raise AdapterAuthError(
                f"Portainer refused the API key on GET {path} ({response.status_code})",
                status_code=response.status_code,
                body=response.text[:300],
            )
        if response.status_code >= 400:
            raise AdapterRequestError(
                f"Portainer answered {response.status_code} on GET {path}: {response.text[:200]}",
                status_code=response.status_code,
                body=response.text[:300],
            )
        return response

    async def _json(
        self, path: str, params: dict[str, Any] | None = None, *, cache: bool = True
    ) -> Any:
        key = (path, tuple(sorted((k, str(v)) for k, v in (params or {}).items())))
        now = time.monotonic()
        if cache and (hit := self._cache.get(key)) and now - hit[0] < CACHE_TTL_S:
            return hit[1]
        data = (await self._request(path, params)).json()
        if cache:
            self._cache[key] = (now, data)
        return data

    def invalidate(self) -> None:
        self._cache.clear()

    # -- checks ----------------------------------------------------------------------------

    async def check(self) -> PortainerCheck:
        if not self.configured:
            return PortainerCheck(
                configured=False,
                reachable=False,
                ok=False,
                detail="not configured (PORTAINER_URL / PORTAINER_API_KEY)",
            )
        try:
            status = await self._json("/api/system/status", cache=False)
            envs = await self._json("/api/endpoints", cache=False)
        except AdapterAuthError as exc:
            return PortainerCheck(True, True, False, detail=str(exc))
        except AdapterRequestError as exc:
            return PortainerCheck(True, True, False, detail=str(exc))
        except (AdapterUnreachable, AdapterNotConfigured) as exc:
            return PortainerCheck(True, False, False, detail=str(exc))
        version = str((status or {}).get("Version", ""))
        edition = ""
        try:
            info = await self._json("/api/system/version", cache=False)
            version = version or str((info or {}).get("ServerVersion", ""))
            edition = str((info or {}).get("ServerEdition", ""))
        except (AdapterRequestError, AdapterUnreachable, AdapterAuthError):
            pass  # older Portainer: /status was enough
        count = len(envs) if isinstance(envs, list) else 0
        return PortainerCheck(
            configured=True,
            reachable=True,
            ok=True,
            version=version,
            edition=edition,
            environments=count,
            detail=f"Portainer {version or '?'}{' ' + edition if edition else ''}, {count} environment(s)",
        )

    # -- environments & stacks ---------------------------------------------------------------

    async def list_environments(self) -> list[Environment]:
        rows = await self._json("/api/endpoints", {"limit": 1000})
        return [parse.environment(r) for r in rows if isinstance(r, dict) and "Id" in r]

    async def _containers_raw(self, endpoint_id: int) -> list[dict[str, Any]]:
        rows = await self._json(
            f"/api/endpoints/{endpoint_id}/docker/containers/json", {"all": "1"}
        )
        return [r for r in rows if isinstance(r, dict)]

    async def list_stacks(self, endpoint_id: int | None = None) -> StackListing:
        listing = StackListing()
        managed = [
            parse.stack(r)
            for r in await self._json("/api/stacks")
            if isinstance(r, dict) and "Id" in r
        ]
        if endpoint_id is not None:
            managed = [s for s in managed if s.endpoint_id == endpoint_id]
        listing.stacks.extend(managed)
        known = {(s.endpoint_id, s.name.lower()) for s in managed}

        envs = [e for e in await self.list_environments() if e.is_docker and e.status != "down"]
        if endpoint_id is not None:
            envs = [e for e in envs if e.id == endpoint_id]

        async def external(env: Environment) -> list[Stack]:
            projects: dict[str, list[parse.Container]] = {}
            for c in await self._containers_raw(env.id):
                cont = parse.container(c)
                if cont.stack and (env.id, cont.stack.lower()) not in known:
                    projects.setdefault(cont.stack, []).append(cont)
            return [
                parse.external_stack(
                    name,
                    env.id,
                    "compose",
                    sum(1 for c in members if c.state == "running"),
                )
                for name, members in sorted(projects.items())
            ]

        results = await asyncio.gather(*(external(e) for e in envs), return_exceptions=True)
        for env, result in zip(envs, results, strict=True):
            if isinstance(result, BaseException):
                listing.errors[env.id] = str(result)
            else:
                listing.stacks.extend(result)
        listing.stacks.sort(key=lambda s: (s.endpoint_id, s.name.lower()))
        return listing

    async def get_stack(self, endpoint_id: int, name: str) -> Stack | None:
        for s in (await self.list_stacks(endpoint_id)).stacks:
            if s.name.lower() == name.lower():
                return s
        return None

    async def stack_kind(self, stack: Stack) -> str:
        if stack.id is None:
            return "unknown"
        try:
            data = await self._json(f"/api/stacks/{stack.id}/file")
        except (AdapterRequestError, AdapterUnreachable, AdapterAuthError):
            return "unknown"
        return parse.classify_compose(str((data or {}).get("StackFileContent", "")))

    # -- containers ---------------------------------------------------------------------------

    async def list_containers(
        self, endpoint_id: int, *, stack: str | None = None, include_stopped: bool = True
    ) -> list[Container]:
        out = [parse.container(r) for r in await self._containers_raw(endpoint_id)]
        if stack is not None:
            out = [c for c in out if (c.stack or "").lower() == stack.lower()]
        if not include_stopped:
            out = [c for c in out if c.state == "running"]
        return sorted(out, key=lambda c: c.name)

    async def _resolve(self, endpoint_id: int, ref: str) -> str | None:
        """A container id from an id prefix or a name."""
        for c in await self._containers_raw(endpoint_id):
            cid = str(c.get("Id", ""))
            if (
                cid == ref
                or (len(ref) >= 6 and cid.startswith(ref))
                or parse.container(c).name == ref
            ):
                return cid
        return None

    async def get_container(self, endpoint_id: int, ref: str) -> ContainerDetail | None:
        cid = await self._resolve(endpoint_id, ref)
        if cid is None:
            return None
        raw = await self._json(
            f"/api/endpoints/{endpoint_id}/docker/containers/{cid}/json", cache=False
        )
        return parse.container_detail(raw)

    async def logs(
        self,
        endpoint_id: int,
        ref: str,
        *,
        tail: int = 200,
        since: int | None = None,
        timestamps: bool = True,
    ) -> list[LogLine]:
        cid = await self._resolve(endpoint_id, ref)
        if cid is None:
            raise AdapterRequestError(
                f"no container {ref!r} on environment {endpoint_id}", status_code=404
            )
        raw = await self._json(
            f"/api/endpoints/{endpoint_id}/docker/containers/{cid}/json", cache=False
        )
        tty = bool((raw.get("Config") or {}).get("Tty"))
        params: dict[str, Any] = {
            "stdout": "1",
            "stderr": "1",
            "tail": str(max(1, min(int(tail), _MAX_TAIL))),
            "timestamps": "1" if timestamps else "0",
        }
        if since is not None:
            params["since"] = str(int(since))
        response = await self._request(
            f"/api/endpoints/{endpoint_id}/docker/containers/{cid}/logs", params
        )
        return parse.parse_log_stream(response.content, tty=tty, timestamps=timestamps)

    async def stats(self, endpoint_id: int, ref: str) -> ContainerStats:
        cid = await self._resolve(endpoint_id, ref)
        if cid is None:
            raise AdapterRequestError(
                f"no container {ref!r} on environment {endpoint_id}", status_code=404
            )
        raw = await self._json(
            f"/api/endpoints/{endpoint_id}/docker/containers/{cid}/stats",
            {"stream": "false"},
            cache=False,
        )
        return parse.container_stats(raw)
