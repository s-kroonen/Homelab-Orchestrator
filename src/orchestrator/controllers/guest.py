"""The Proxmox guest as a controller: start / stop / restart the whole VM or container."""

from __future__ import annotations

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.proxmox.base import ProxmoxAdapter
from orchestrator.controllers.base import (
    ControllerError,
    ControllerStatus,
    ControllerUnavailable,
    ServiceController,
)
from orchestrator.domain.enums import GuestKind

_TASK_TIMEOUT_S = 180


class GuestController(ServiceController):
    type = "guest"

    def __init__(self, proxmox: ProxmoxAdapter, node: str, kind: GuestKind, vmid: int) -> None:
        self._proxmox, self._node, self._kind, self._vmid = proxmox, node, kind, vmid

    def describe(self) -> str:
        return f"Proxmox {self._kind.value} {self._vmid} on {self._node}"

    async def status(self) -> ControllerStatus:
        try:
            guests = await self._proxmox.list_guests(self._node)
        except AdapterError as exc:
            raise ControllerUnavailable(str(exc)) from exc
        guest = next((g for g in guests if g.vmid == self._vmid), None)
        if guest is None:
            raise ControllerError(f"guest {self._vmid} was not found on {self._node}")
        return ControllerStatus(guest.status == "running", guest.status)

    async def _task(self, verb: str) -> str:
        try:
            if verb == "start":
                handle = await self._proxmox.start_guest(self._node, self._vmid, self._kind)
            else:
                handle = await self._proxmox.stop_guest(self._node, self._vmid, self._kind)
            done = await self._proxmox.wait_for_task(
                self._node, handle.upid, timeout_s=_TASK_TIMEOUT_S
            )
        except AdapterError as exc:
            raise ControllerError(f"{verb} of {self.describe()} failed: {exc}") from exc
        if not done.succeeded:
            raise ControllerError(
                f"{verb} of {self.describe()} failed: {done.exit_status or 'unknown'}"
            )
        return f"{verb} done ({self.describe()})"

    async def start(self) -> str:
        return await self._task("start")

    async def stop(self) -> str:
        return await self._task("stop")

    async def restart(self) -> str:
        await self._task("stop")
        return await self._task("start")
