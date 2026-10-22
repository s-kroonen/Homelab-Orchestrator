"""Portainer: containers (and stacks) through its HTTP API.

``controller: {type: portainer, endpoint: 2, container: nginx}`` or
``{type: portainer, endpoint: 2, stack: media}``. ``PORTAINER_URL`` and
``PORTAINER_API_KEY`` (a Portainer access token; sent as ``X-API-Key``) come from
the environment.
"""

from __future__ import annotations

import json
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

    def __init__(self, settings: Settings, cfg: dict[str, Any]) -> None:
        self._base = settings.portainer_url.strip().rstrip("/")
        self._key = settings.portainer_api_key.get_secret_value()
        self._verify = settings.portainer_verify_tls
        self._timeout = settings.controller_timeout_s
        self._endpoint = int(cfg["endpoint"])
        self._container = str(cfg.get("container") or "")
        self._stack_name = str(cfg.get("stack") or "")

    def describe(self) -> str:
        what = f"container {self._container}" if self._container else f"stack {self._stack_name}"
        return f"Portainer endpoint {self._endpoint}, {what}"

    async def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        if not self._base or not self._key:
            raise ControllerError(
                "Portainer is not configured - set PORTAINER_URL and PORTAINER_API_KEY"
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

    async def _container_action(self, verb: str) -> str:
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

    async def _stack_action(self, verb: str) -> str:
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
        stack = await self._stack()
        active = stack.get("Status") == 1
        return ControllerStatus(active, "active" if active else "inactive")

    async def start(self) -> str:
        return await (
            self._container_action("start") if self._container else self._stack_action("start")
        )

    async def stop(self) -> str:
        return await (
            self._container_action("stop") if self._container else self._stack_action("stop")
        )

    async def restart(self) -> str:
        if self._container:
            return await self._container_action("restart")
        await self._stack_action("stop")
        return await self._stack_action("start")
