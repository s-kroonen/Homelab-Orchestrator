"""Connectors: one per kind of source (Proxmox, Portainer, a proxy...). See base.py and
docs/connectors.md."""

from orchestrator.connectors.base import Connector, SourceCtx
from orchestrator.connectors.registry import (
    Source,
    all_sources,
    connector_for,
    connectors,
    context,
    default_source,
    get_source,
    sources_of,
)

__all__ = [
    "Connector",
    "Source",
    "SourceCtx",
    "all_sources",
    "connector_for",
    "connectors",
    "context",
    "default_source",
    "get_source",
    "sources_of",
]
