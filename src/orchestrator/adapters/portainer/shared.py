"""The Portainer adapter the running process uses, for code that is not handed the adapter
bundle (the health probe). ``build_adapters`` registers it."""

from __future__ import annotations

from orchestrator.adapters.portainer.base import PortainerAdapter

_shared: PortainerAdapter | None = None


def set_shared(adapter: PortainerAdapter | None) -> None:
    global _shared
    _shared = adapter


def get_shared() -> PortainerAdapter | None:
    return _shared
