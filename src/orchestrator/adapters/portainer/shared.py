"""The adapters the running process uses, for code that is not handed the adapter bundle (the
health probe). ``build_adapters`` registers the bundle; ``reload`` re-registers it."""

from __future__ import annotations

from typing import Any

from orchestrator.adapters.portainer.base import PortainerAdapter

_bundle: Any = None


def set_shared(bundle: Any) -> None:
    global _bundle
    _bundle = bundle


def get_shared(instance: str | None = None) -> PortainerAdapter | None:
    """The Portainer a connection id names (blank: the default one)."""
    if _bundle is None:
        return None
    adapter: PortainerAdapter | None = _bundle.portainer_for(instance)
    return adapter
