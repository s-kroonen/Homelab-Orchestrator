"""Live Portainer adapter: read-only, through the HTTP API.

Authenticates with an access token (``X-API-Key``). Container data comes through
Portainer's Docker proxy (``/api/endpoints/{id}/docker/...``), so one key reaches every
environment Portainer manages.
"""

from __future__ import annotations

import asyncio
import re
import ssl
import time
from collections.abc import Callable
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
    ExecResult,
    LogLine,
    PortainerAdapter,
    PortainerCheck,
    Stack,
    StackCreate,
    StackListing,
)
from orchestrator.config import Settings
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

#: Repeated reads inside this window (a page load asks for the same list several times)
#: are served from memory. Logs and stats are never cached.
CACHE_TTL_S = 5.0
_MAX_TAIL = 2000
#: How much of a command's stderr is kept.
_STDERR_KEEP = 2000


def _is_tls_failure(exc: BaseException) -> bool:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(cause):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _html_reply(text: str) -> str | None:
    """A web page where JSON was expected means something in front of Portainer (a reverse
    proxy's error page) answered, not Portainer. Describe it instead of dumping its markup."""
    head = text.lstrip()[:200].lower()
    if not (head.startswith("<!doctype html") or head.startswith("<html")):
        return None
    found = _TITLE.search(text)
    title = " ".join(found.group(1).split())[:80] if found else "no title"
    return (
        f"an HTML page ({title!r}) came back instead of Portainer's reply: a reverse proxy in "
        "front of Portainer answered. That is usually Traefik replacing a 5xx from Portainer "
        "(often an environment that is down) with its 'not available' page. Point PORTAINER_URL "
        "at Portainer itself (https://<host>:9443) to see its own message"
    )


def _error_text(response: httpx.Response) -> str:
    """Portainer's own explanation (``{"message", "details"}``) when it gave one."""
    try:
        body = response.json()
    except ValueError:
        return _html_reply(response.text) or response.text
    if isinstance(body, dict):
        message = " - ".join(str(body[k]) for k in ("message", "details") if body.get(k))
        if message:
            return message
    return response.text


class LivePortainerAdapter(PortainerAdapter):
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._url = settings.portainer_url.strip().rstrip("/")
        self._public = settings.portainer_public_url.strip().rstrip("/") or self._url
        self._key = settings.portainer_api_key.get_secret_value()
        self._verify = settings.portainer_verify_tls
        self._timeout = float(settings.controller_timeout_s)
        self._read_only = settings.portainer_read_only
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

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        json: Any = None,
    ) -> httpx.Response:
        if not self.configured:
            raise AdapterNotConfigured(
                "Portainer is not configured - set PORTAINER_URL and PORTAINER_API_KEY"
            )
        if self._client is None:
            await self.start()
        assert self._client is not None
        try:
            response = await self._client.request(method, path, params=params, json=json)
        except httpx.TimeoutException as exc:
            raise AdapterUnreachable(f"Portainer timed out on {method} {path}: {exc}") from exc
        except httpx.TransportError as exc:
            if _is_tls_failure(exc):
                raise AdapterTlsError(
                    f"Portainer's TLS certificate was rejected on {method} {path}: {exc}. The host "
                    "answered - this is certificate trust. Set PORTAINER_VERIFY_TLS=false or "
                    "trust its CA."
                ) from exc
            raise AdapterUnreachable(f"Portainer unreachable on {method} {path}: {exc}") from exc
        if response.status_code in (401, 403):
            raise AdapterAuthError(
                f"Portainer refused the API key on {method} {path} ({response.status_code})"
                + (
                    " - writing needs a user with access to the environment"
                    if method != "GET"
                    else ""
                ),
                status_code=response.status_code,
                body=_error_text(response),
            )
        if response.status_code >= 400:
            raise AdapterRequestError(
                f"Portainer answered {response.status_code} on {method} {path}: "
                f"{_error_text(response)[:450]}",
                status_code=response.status_code,
                body=_error_text(response)[:300],
            )
        return response

    def _writable(self) -> None:
        if self._read_only:
            raise AdapterRequestError(
                "Portainer is read-only here (PORTAINER_READ_ONLY=true): nothing is changed."
            )

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
        all_envs = await self.list_environments()
        # Portainer keeps a stack's record after its environment goes away, still "active".
        # An environment that is down is not shown, and neither are its stacks.
        down = {e.id for e in all_envs if e.status == "down"}
        for eid in sorted({s.endpoint_id for s in managed} & down):
            listing.errors[eid] = "Portainer reports this environment as down"
        managed = [s for s in managed if s.endpoint_id not in down]
        listing.stacks.extend(managed)
        known = {(s.endpoint_id, s.name.lower()) for s in managed}

        envs = [e for e in all_envs if e.is_docker and e.status != "down"]
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

    # -- writes ------------------------------------------------------------------------------

    async def _raw_stack(self, stack_id: int) -> dict[str, Any]:
        raw = await self._json(f"/api/stacks/{stack_id}", cache=False)
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _need_id(stack: Stack) -> int:
        if stack.id is None:
            raise AdapterRequestError(
                f"{stack.name!r} is a compose project Portainer did not create, so Portainer "
                "cannot start, stop, redeploy or delete it as a stack"
            )
        return stack.id

    async def start_stack(self, stack: Stack) -> None:
        self._writable()
        sid = self._need_id(stack)
        await self._request(
            f"/api/stacks/{sid}/start", {"endpointId": stack.endpoint_id}, method="POST"
        )
        self.invalidate()

    async def stop_stack(self, stack: Stack) -> None:
        self._writable()
        sid = self._need_id(stack)
        await self._request(
            f"/api/stacks/{sid}/stop", {"endpointId": stack.endpoint_id}, method="POST"
        )
        self.invalidate()

    async def redeploy_stack(
        self, stack: Stack, *, pull_image: bool = True, prune: bool = False
    ) -> None:
        self._writable()
        sid = self._need_id(stack)
        if stack.git is None:
            raise AdapterRequestError(
                f"{stack.name!r} was not deployed from a Git repository, so there is nothing "
                "to re-pull; redeploy it in Portainer (web editor stacks have no Git source)"
            )
        raw = await self._raw_stack(sid)
        git = raw.get("GitConfig") or {}
        auth = git.get("Authentication") or {}
        body: dict[str, Any] = {
            # Portainer REPLACES the stack's environment with this list: send the current
            # one back untouched, or a redeploy would wipe it. It goes nowhere else.
            "env": raw.get("Env") or [],
            "prune": prune,
            "pullImage": pull_image,
            "repositoryReferenceName": git.get("ReferenceName", ""),
            "repositoryAuthentication": bool(auth),
        }
        if auth.get("GitCredentialID"):
            body["repositoryGitCredentialID"] = auth["GitCredentialID"]
        if auth.get("Username"):
            body["repositoryUsername"] = auth["Username"]
        if auth.get("Password"):
            body["repositoryPassword"] = auth["Password"]
        await self._request(
            f"/api/stacks/{sid}/git/redeploy",
            {"endpointId": stack.endpoint_id},
            method="PUT",
            json=body,
        )
        self.invalidate()

    def _create_request(self, spec: StackCreate) -> tuple[str, dict[str, Any], dict[str, Any]]:
        env = [{"name": n, "value": v} for n, v in spec.env]
        if spec.source == "string":
            if not spec.compose_text.strip():
                raise AdapterRequestError("a stack from text needs the compose text")
            return (
                "/api/stacks/create/standalone/string",
                {"endpointId": spec.endpoint_id},
                {"name": spec.name, "stackFileContent": spec.compose_text, "env": env},
            )
        if spec.source != "git":
            raise AdapterRequestError("a stack comes from a Git repository or from compose text")
        if not spec.repository_url.strip():
            raise AdapterRequestError("a stack from Git needs the repository URL")
        has_auth = bool(spec.git_credential_id or spec.git_username or spec.git_password)
        body: dict[str, Any] = {
            "name": spec.name,
            "repositoryURL": spec.repository_url,
            "repositoryReferenceName": spec.reference,
            "composeFile": spec.compose_path,
            "repositoryAuthentication": has_auth,
            "env": env,
            "autoUpdate": (
                {"interval": spec.auto_update_interval or "5m", "webhook": ""}
                if spec.auto_update
                else None
            ),
        }
        if spec.git_credential_id:
            body["repositoryGitCredentialID"] = spec.git_credential_id
        elif has_auth:
            body["repositoryUsername"] = spec.git_username
            body["repositoryPassword"] = spec.git_password
        return (
            "/api/stacks/create/standalone/repository",
            {"endpointId": spec.endpoint_id},
            body,
        )

    def preview_create(self, spec: StackCreate) -> dict[str, Any]:
        path, params, body = self._create_request(spec)
        shown = dict(body)
        shown["env"] = [{"name": e["name"], "value": "***"} for e in body["env"]]
        if "repositoryPassword" in shown:
            shown["repositoryPassword"] = "***"
        if "stackFileContent" in shown:
            lines = str(shown["stackFileContent"]).count("\n") + 1
            shown["stackFileContent"] = f"<{lines} line(s) of compose text>"
        return {"method": "POST", "path": path, "params": params, "body": shown}

    async def create_stack(self, spec: StackCreate) -> Stack:
        self._writable()
        path, params, body = self._create_request(spec)
        response = await self._request(path, params, method="POST", json=body)
        self.invalidate()
        data = response.json()
        return (
            parse.stack(data)
            if isinstance(data, dict) and "Id" in data
            else Stack(
                id=None,
                name=spec.name,
                endpoint_id=spec.endpoint_id,
                type="compose",
                status="active",
            )
        )

    async def delete_stack(self, stack: Stack) -> None:
        self._writable()
        sid = self._need_id(stack)
        await self._request(
            f"/api/stacks/{sid}",
            {"endpointId": stack.endpoint_id, "external": "false", "removeVolumes": "false"},
            method="DELETE",
        )
        self.invalidate()

    # -- containers ---------------------------------------------------------------------------

    async def container_action(self, endpoint_id: int, ref: str, verb: str) -> str:
        if verb not in {"start", "stop", "restart"}:
            raise AdapterRequestError(f"unknown container action {verb!r}")
        self._writable()
        cid = await self._resolve(endpoint_id, ref)
        if cid is None:
            raise AdapterRequestError(
                f"no container {ref!r} on Portainer environment {endpoint_id}"
            )
        params = {"t": "10"} if verb in {"stop", "restart"} else None
        await self._request(
            f"/api/endpoints/{endpoint_id}/docker/containers/{cid}/{verb}",
            params,
            method="POST",
        )
        self.invalidate()
        return f"{verb} sent to container {ref}"

    async def exec_stream(
        self,
        endpoint_id: int,
        ref: str,
        cmd: list[str],
        sink: Callable[[bytes], None],
        *,
        timeout_s: float = 1800.0,
    ) -> ExecResult:
        self._writable()
        if not self.configured:
            raise AdapterNotConfigured(
                "Portainer is not configured - set PORTAINER_URL and PORTAINER_API_KEY"
            )
        cid = await self._resolve(endpoint_id, ref)
        if cid is None:
            raise AdapterRequestError(
                f"no container {ref!r} on Portainer environment {endpoint_id}"
            )
        base = f"/api/endpoints/{endpoint_id}/docker"
        created = (
            await self._request(
                f"{base}/containers/{cid}/exec",
                method="POST",
                json={"AttachStdout": True, "AttachStderr": True, "Tty": False, "Cmd": cmd},
            )
        ).json()
        exec_id = str((created or {}).get("Id") or "")
        if not exec_id:
            raise AdapterRequestError("Portainer did not return an exec id")
        if self._client is None:
            await self.start()
        assert self._client is not None
        demux = parse.FrameDemux()
        errors = bytearray()
        total = 0

        def take(frames: list[tuple[str, bytes]]) -> None:
            nonlocal total
            for stream, payload in frames:
                if stream == "stderr":
                    errors.extend(payload)
                    del errors[:-_STDERR_KEEP]
                else:
                    total += len(payload)
                    sink(payload)

        try:
            async with self._client.stream(
                "POST",
                f"{base}/exec/{exec_id}/start",
                json={"Detach": False, "Tty": False},
                timeout=httpx.Timeout(self._timeout, read=timeout_s),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:200]
                    raise AdapterRequestError(
                        f"Portainer answered {response.status_code} starting the command: {body}",
                        status_code=response.status_code,
                    )
                async for chunk in response.aiter_raw():
                    take(demux.feed(chunk))
            take(demux.finish())
        except httpx.TimeoutException as exc:
            raise AdapterUnreachable(f"Portainer timed out while the command ran: {exc}") from exc
        except httpx.TransportError as exc:
            raise AdapterUnreachable(f"Portainer connection dropped mid-command: {exc}") from exc
        info = await self._json(f"{base}/exec/{exec_id}/json", cache=False)
        code = (info or {}).get("ExitCode")
        return ExecResult(
            exit_code=int(code) if isinstance(code, int) else None,
            stderr=errors.decode("utf-8", "replace").strip(),
            stdout_bytes=total,
        )

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
