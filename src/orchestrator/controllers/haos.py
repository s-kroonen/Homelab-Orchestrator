"""Home Assistant: add-ons (via the Supervisor) and the Core itself.

``controller: {type: haos, addon: core_mosquitto}`` or ``{type: haos, core: true}``.
``HAOS_URL`` and ``HAOS_TOKEN`` (a long-lived access token of an ADMIN user) come
from the environment. Add-ons go through Home Assistant's Supervisor proxy
(``/api/hassio/...``), which needs that admin token.
"""

from __future__ import annotations

from typing import Any

import httpx

from orchestrator.config import Settings
from orchestrator.controllers.base import (
    ControllerError,
    ControllerStatus,
    ControllerUnavailable,
    ServiceController,
)


class HomeAssistantController(ServiceController):
    type = "haos"

    def __init__(self, settings: Settings, cfg: dict[str, Any]) -> None:
        self._base = settings.haos_url.strip().rstrip("/")
        self._token = settings.haos_token.get_secret_value()
        self._verify = settings.haos_verify_tls
        self._timeout = settings.controller_timeout_s
        self._addon = str(cfg.get("addon") or "")
        self._core = bool(cfg.get("core"))

    def describe(self) -> str:
        return f"Home Assistant add-on {self._addon}" if self._addon else "Home Assistant Core"

    async def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        if not self._base or not self._token:
            raise ControllerError("Home Assistant is not configured - set HAOS_URL and HAOS_TOKEN")
        try:
            async with httpx.AsyncClient(
                base_url=self._base,
                headers={"Authorization": f"Bearer {self._token}"},
                verify=self._verify,
                timeout=self._timeout,
            ) as client:
                response = await client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise ControllerUnavailable(f"could not reach Home Assistant: {exc}") from exc
        if response.status_code in (401, 403):
            raise ControllerError("Home Assistant refused the token (an admin user's is needed)")
        return response

    async def status(self) -> ControllerStatus:
        if self._core:
            r = await self._call("GET", "/api/")
            return ControllerStatus(
                r.status_code == 200, "running" if r.status_code == 200 else str(r.status_code)
            )
        r = await self._call("GET", f"/api/hassio/addons/{self._addon}/info")
        if r.status_code >= 400:
            raise ControllerError(f"Home Assistant answered {r.status_code}: {r.text[:200]}")
        state = str((r.json().get("data") or {}).get("state", "unknown"))
        return ControllerStatus(state == "started", state)

    async def _addon_action(self, verb: str) -> str:
        r = await self._call("POST", f"/api/hassio/addons/{self._addon}/{verb}")
        if r.status_code >= 400:
            raise ControllerError(f"Home Assistant {verb} answered {r.status_code}: {r.text[:200]}")
        return f"{verb} sent to add-on {self._addon}"

    async def start(self) -> str:
        if self._core:
            raise ControllerError(
                "Home Assistant Core cannot be started from here (it is what answers)"
            )
        return await self._addon_action("start")

    async def stop(self) -> str:
        if self._core:
            raise ControllerError(
                "stopping Home Assistant Core from here would cut the line - use restart"
            )
        return await self._addon_action("stop")

    async def restart(self) -> str:
        if self._core:
            r = await self._call("POST", "/api/services/homeassistant/restart")
            if r.status_code >= 400:
                raise ControllerError(
                    f"Home Assistant restart answered {r.status_code}: {r.text[:200]}"
                )
            return "restart sent to Home Assistant Core"
        return await self._addon_action("restart")
