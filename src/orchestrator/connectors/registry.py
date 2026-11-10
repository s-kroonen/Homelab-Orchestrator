"""Every source the orchestrator has: one per connection of a kind that has a connector.

``all_sources()`` lists them (from ``connections.yaml`` and, for kinds not moved there yet,
``.env``); ``get_source(id)`` finds one; ``context(...)`` gives a connector what it needs to
look at one. Kinds without a connector (the alert channels, the SSH settings) are not sources:
they live on Settings → Notifications and Settings → General.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.config import Settings
from orchestrator.connections import runtime
from orchestrator.connections.model import Connection
from orchestrator.connectors.base import Connector, SourceCtx

_CONNECTORS: dict[str, Connector] = {}


def register(connector: Connector) -> Connector:
    _CONNECTORS[connector.kind] = connector
    return connector


def connectors() -> list[Connector]:
    _load()
    return list(_CONNECTORS.values())


def connector_for(kind: str) -> Connector | None:
    _load()
    return _CONNECTORS.get(kind)


_loaded = False


def _load() -> None:
    """Import the connector modules once (each registers itself)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    from orchestrator.connectors import kinds  # noqa: F401


@dataclass
class Source:
    conn: Connection
    connector: Connector

    @property
    def id(self) -> str:
        return self.conn.id

    @property
    def name(self) -> str:
        return self.conn.name or self.conn.id

    @property
    def kind(self) -> str:
        return self.conn.kind

    @property
    def enabled(self) -> bool:
        return self.conn.enabled

    @property
    def href(self) -> str:
        return f"/sources/{self.conn.id}"

    def public(self) -> dict[str, Any]:
        c = self.connector
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "kind_title": c.title,
            "group": c.group,
            "family": c.family,
            "enabled": self.enabled,
            "default": self.conn.default,
            "configured": self.conn.configured,
            "from_env": self.conn.source == "env",
            "address": c.address(self.conn),
            "href": self.href,
        }


def all_sources(settings: Settings | None = None) -> list[Source]:
    """Every source, in the order connectors are registered, then by name."""
    _load()
    snap = runtime.current()
    out = [Source(conn, _CONNECTORS[conn.kind]) for conn in snap.all if conn.kind in _CONNECTORS]
    order = {k: i for i, k in enumerate(_CONNECTORS)}
    return sorted(out, key=lambda s: (order[s.kind], s.name))


def get_source(ident: str) -> Source | None:
    return next((s for s in all_sources() if s.id == ident), None)


def sources_of(kind: str) -> list[Source]:
    return [s for s in all_sources() if s.kind == kind]


def default_source(kind: str) -> Source | None:
    """The source of ``kind`` used where nothing names one (the default connection)."""
    conn = runtime.current().primary(kind)
    return get_source(conn.id) if conn is not None else None


def context(source: Source, settings: Settings, adapters: Any = None) -> SourceCtx:
    adapter = adapters.for_source(source.id) if adapters is not None and source.enabled else None
    return SourceCtx(
        conn=source.conn,
        settings=runtime.settings_for(settings, source.conn),
        adapter=adapter,
        adapters=adapters,
    )


__all__ = [
    "Source",
    "all_sources",
    "connector_for",
    "connectors",
    "context",
    "default_source",
    "get_source",
    "register",
    "sources_of",
]
