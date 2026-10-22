"""Controllers: how to act on ONE service itself, without touching its guest.

Same idea as the proxy and power adapters: a small interface, one implementation
per kind of thing (a Portainer container, a Home Assistant add-on, a systemd unit,
a Proxmox guest), chosen by the service's ``controller:`` block.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ControllerError(Exception):
    """The controller refused or the thing could not be controlled (shown to the operator)."""


class ControllerUnavailable(ControllerError):
    """The controlling API/host could not be reached."""


@dataclass(frozen=True)
class ControllerStatus:
    #: True / False when the controller can tell; None = could not tell.
    running: bool | None
    state: str  # the controller's own word: "running", "exited", "started", ...
    detail: str = ""


class ServiceController(ABC):
    #: The ``type:`` this implements.
    type: str = ""

    def describe(self) -> str:
        """One line for the UI: what exactly is being controlled."""
        return self.type

    @abstractmethod
    async def status(self) -> ControllerStatus: ...

    @abstractmethod
    async def start(self) -> str:
        """Start it; returns a short human message. Raises ControllerError."""

    @abstractmethod
    async def stop(self) -> str: ...

    @abstractmethod
    async def restart(self) -> str: ...
