"""Portainer: containers (and stacks) through its HTTP API.

``controller: {type: portainer, endpoint: 2, container: nginx}`` or
``{type: portainer, endpoint: 2, stack: media}``. ``PORTAINER_URL`` and
``PORTAINER_API_KEY`` (a Portainer access token; sent as ``X-API-Key``) come from
the environment.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx

from orchestrator.config import Settings
from orchestrator.controllers.base import (
    ControllerError,
    ControllerStatus,
    ControllerUnavailable,
    ServiceController,
)


class PortainerController(ServiceController):
    type = "portainer"

    def __init__(
        self, settings: Settings, cfg: dict[str, Any], *, needs: Sequence[str] = ()
    ) -> None:
        self._instance = str(cfg.get("instance") or "").strip()
        if self._instance:  # a particular Portainer connection, not the default one
            from orchestrator.connections import runtime

            conn = runtime.find_instance(settings, "portainer", self._instance)
            if conn is None:
                raise ControllerError(f"there is no Portainer connection called {self._instance!r}")
            settings = runtime.settings_for(settings, conn)
        self._base = settings.portainer_url.strip().rstrip("/")
        self._key = settings.portainer_api_key.get_secret_value()
        self._verify = settings.portainer_verify_tls
        self._timeout = settings.controller_timeout_s
        self._read_only = settings.portainer_read_only
        self._endpoint = int(cfg["endpoint"])
        self._container = str(cfg.get("container") or "")
        self._stack_name = str(cfg.get("stack") or "")
        #: For a stack: the containers the service needs (its required container checks).
        #: Status and start look at only these; empty = every container of the stack.
        self._needs = tuple(n for n in needs if n)

    def describe(self) -> str:
        what = f"container {self._container}" if self._container else f"stack {self._stack_name}"
        where = f"Portainer {self._instance}" if self._instance else "Portainer"
        return f"{where} endpoint {self._endpoint}, {what}"

    async def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        if not self._base or not self._key:
            raise ControllerError(
                "Portainer is not configured - add it under Sources "
                "(or set PORTAINER_URL and PORTAINER_API_KEY)"
            )
        try:
            async with httpx.AsyncClient(
                base_url=self._base,
                headers={"X-API-Key": self._key},
                verify=self._verify,
                timeout=self._timeout,
            ) as client:
                response = await client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise ControllerUnavailable(f"could not reach Portainer: {exc}") from exc
        if response.status_code in (401, 403):
            raise ControllerError("Portainer refused the API key")
        return response

    # -- containers ----------------------------------------------------------

    async def _container_id(self) -> tuple[str, str]:
        """(id, state) of the container by name."""
        filters = json.dumps({"name": [f"^/{self._container}$"]})
        r = await self._call(
            "GET",
            f"/api/endpoints/{self._endpoint}/docker/containers/json",
            params={"all": "1", "filters": filters},
        )
        if r.status_code >= 400:
            raise ControllerError(f"Portainer answered {r.status_code}: {r.text[:200]}")
        rows = r.json()
        if not rows:
            raise ControllerError(
                f"no container named {self._container!r} on Portainer endpoint {self._endpoint}"
            )
        return str(rows[0]["Id"]), str(rows[0].get("State", ""))

    def _writable(self) -> None:
        if self._read_only:
            raise ControllerError(
                "Portainer is read-only here (PORTAINER_READ_ONLY=true): nothing is changed."
            )

    async def _container_action(self, verb: str) -> str:
        self._writable()
        cid, _ = await self._container_id()
        params = {"t": "10"} if verb in {"stop", "restart"} else None
        r = await self._call(
            "POST",
            f"/api/endpoints/{self._endpoint}/docker/containers/{cid}/{verb}",
            params=params,
        )
        # 204 = done, 304 = already in that state.
        if r.status_code not in (204, 304, 200):
            raise ControllerError(f"Portainer {verb} answered {r.status_code}: {r.text[:200]}")
        return f"{verb} sent to container {self._container}" + (
            " (it already was)" if r.status_code == 304 else ""
        )

    # -- stacks --------------------------------------------------------------

    async def _stack(self) -> dict[str, Any]:
        r = await self._call("GET", "/api/stacks")
        if r.status_code >= 400:
            raise ControllerError(f"Portainer answered {r.status_code}: {r.text[:200]}")
        for stack in r.json():
            if stack.get("Name") == self._stack_name and stack.get("EndpointId") == self._endpoint:
                return dict(stack)
        raise ControllerError(f"no stack named {self._stack_name!r} on endpoint {self._endpoint}")

    async def _stack_boxes(self) -> list[tuple[str, str, str]]:
        """(id, name, state) of every container of the stack (its compose project)."""
        filters = json.dumps({"label": [f"com.docker.compose.project={self._stack_name}"]})
        r = await self._call(
            "GET",
            f"/api/endpoints/{self._endpoint}/docker/containers/json",
            params={"all": "1", "filters": filters},
        )
        if r.status_code >= 400:
            raise ControllerError(f"Portainer answered {r.status_code}: {r.text[:200]}")
        out = []
        for row in r.json():
            names = row.get("Names") or []
            name = str(names[0]).lstrip("/") if names else str(row.get("Id", ""))[:12]
            out.append((str(row.get("Id", "")), name, str(row.get("State", ""))))
        return out

    def _wanted(
        self, boxes: list[tuple[str, str, str]]
    ) -> tuple[list[tuple[str, str, str]], list[str]]:
        """The containers that count (the needed ones, else all) and needed names not found."""
        if not self._needs:
            return boxes, []
        have = {b[1] for b in boxes}
        return [b for b in boxes if b[1] in self._needs], [n for n in self._needs if n not in have]

    async def _stack_status(self) -> ControllerStatus:
        stack = await self._stack()
        if stack.get("Status") != 1:
            return ControllerStatus(False, "inactive")
        wanted, missing = self._wanted(await self._stack_boxes())
        stopped = [name for _, name, state in wanted if state != "running"]
        if missing or stopped or not wanted:
            detail = "; ".join(
                part
                for part in (
                    f"stopped: {', '.join(stopped)}" if stopped else "",
                    f"not found: {', '.join(missing)}" if missing else "",
                    "no containers" if not wanted and not missing else "",
                )
                if part
            )
            return ControllerStatus(False, "active", detail)
        return ControllerStatus(True, "active", f"{len(wanted)} container(s) running")

    async def _stack_start(self) -> str:
        """A stopped stack is started as a whole (that is how Portainer brings it up). In a
        running stack only the stopped containers that count are started, nothing else."""
        stack = await self._stack()
        if stack.get("Status") != 1:
            return await self._stack_action("start")
        self._writable()
        wanted, missing = self._wanted(await self._stack_boxes())
        if missing:
            raise ControllerError(f"stack {self._stack_name} has no container {', '.join(missing)}")
        stopped = [(cid, name) for cid, name, state in wanted if state != "running"]
        if not stopped:
            return f"stack {self._stack_name} already running"
        for cid, name in stopped:
            r = await self._call(
                "POST", f"/api/endpoints/{self._endpoint}/docker/containers/{cid}/start"
            )
            if r.status_code not in (204, 304, 200):
                raise ControllerError(
                    f"Portainer start of {name} answered {r.status_code}: {r.text[:200]}"
                )
        names = ", ".join(name for _, name in stopped)
        return f"started {names} in stack {self._stack_name} (nothing else)"

    async def _stack_action(self, verb: str) -> str:
        self._writable()
        stack = await self._stack()
        r = await self._call(
            "POST", f"/api/stacks/{stack['Id']}/{verb}", params={"endpointId": self._endpoint}
        )
        if r.status_code not in (200, 204):
            raise ControllerError(f"Portainer {verb} answered {r.status_code}: {r.text[:200]}")
        return f"{verb} sent to stack {self._stack_name}"

    # -- interface -------------------------------------------------------------

    async def status(self) -> ControllerStatus:
        if self._container:
            _, state = await self._container_id()
            return ControllerStatus(state == "running", state or "unknown")
        return await self._stack_status()

    async def start(self) -> str:
        return await (self._container_action("start") if self._container else self._stack_start())

    async def stop(self) -> str:
        return await (
            self._container_action("stop") if self._container else self._stack_action("stop")
        )

    async def restart(self) -> str:
        if self._container:
            return await self._container_action("restart")
        await self._stack_action("stop")
        return await self._stack_action("start")
