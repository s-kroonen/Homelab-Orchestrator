"""The connector standard: what every kind of source declares (OR-77, docs/connectors.md).

A *connector* is one kind of thing the orchestrator talks to (Proxmox VE, Portainer, a Traefik
Manager...). A *source* is one instance of it: a connection in ``connections.yaml``. The
connector says, for any of its sources:

* ``summary``: one line for lists ("3 nodes · 21 guests") and the status;
* ``overview``: tiles, what needs attention and facts, for the Overview tab;
* ``tabs``: the tabs of its page (Overview, its resources, Settings, Raw);
* ``resources``: the lists it has (nodes, stacks, proxy hosts...), with their columns, and the
  actions a row or a selection of rows can take;
* ``raw``: endpoints shown as they are on the Raw tab, read only, secrets redacted;
* ``links``: what its things are in another source ("this VM hosts that Portainer environment").

The fields of a source, its secrets and its check come from its kind in
``connections/kinds.py`` and ``connections/checks.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from orchestrator.config import Settings
from orchestrator.connections.model import Connection

# -- what a page is made of ---------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    #: How the page shows it: text, mono, pill, ago, time, pct, bytes, num, link.
    kind: str = "text"


@dataclass(frozen=True)
class Chip:
    key: str
    label: str
    #: The row field that must be truthy for the row to match (rows carry ``chips``: a list).
    field: str = ""


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    danger: bool = False
    #: Can several selected rows take it at once (the bulk bar)?
    bulk: bool = False
    #: Ask before doing it; ``typed`` also asks for the row's name to be typed.
    confirm: bool = False
    typed: bool = False


@dataclass(frozen=True)
class Resource:
    """One list a source has. ``fetch`` returns plain dicts: ``id`` and the column keys, plus
    ``chips`` (keys of the chips the row matches), ``links`` (``[{label, href}]``) and ``href``
    (the row's own page, when it has one)."""

    key: str
    label: str
    columns: tuple[Column, ...]
    fetch: Callable[[SourceCtx], Awaitable[list[dict[str, Any]]]]
    chips: tuple[Chip, ...] = ()
    actions: tuple[Action, ...] = ()
    #: Does the page draw it from ``columns`` (generic), or does a page module of its own?
    view: str = "generic"


@dataclass(frozen=True)
class RawEndpoint:
    key: str
    label: str
    #: What the request is, for the page ("GET /api2/json/nodes").
    describe: str
    fetch: Callable[[SourceCtx], Awaitable[Any]]


@dataclass(frozen=True)
class Tab:
    key: str
    label: str
    #: "overview", "settings", "raw", "resource:<key>", or a page module's own view name.
    view: str


@dataclass
class Summary:
    """One source, in a line: whether it answers and what it has."""

    #: ok / warn / fail / off (switched off) / unknown (not checked, dry run).
    state: str = "unknown"
    #: "reachable", "unreachable", "datastore 91% full"...
    label: str = ""
    has: str = ""
    version: str = ""
    ms: int | None = None
    detail: str = ""


@dataclass
class Overview:
    tiles: list[dict[str, Any]] = field(default_factory=list)
    attention: list[dict[str, Any]] = field(default_factory=list)
    facts: list[tuple[str, str]] = field(default_factory=list)


# -- one source at work -------------------------------------------------------------------------


@dataclass
class SourceCtx:
    """Everything a connector needs to look at one of its sources."""

    conn: Connection
    #: The orchestrator's settings with this source's values (``runtime.settings_for``).
    settings: Settings
    #: This source's adapter (None: switched off, or a kind without one).
    adapter: Any = None
    #: The whole adapter bundle, for links into other sources.
    adapters: Any = None

    @property
    def id(self) -> str:
        return self.conn.id


class Connector:
    """Base for every kind of source. Subclasses set the class attributes and override what
    they have; the defaults make a source with Overview, Settings and Raw only."""

    #: The connection kind (``connections/kinds.py``).
    kind: str = ""
    #: "Proxmox VE", "Portainer"...
    title: str = ""
    #: The sidebar group: "Proxmox", "Portainer", "Proxy", "Backups", "Power", "Machines"...
    group: str = ""
    #: The Add source drawer's section: machines, apps, network.
    family: str = "apps"
    #: One line for the Add source drawer.
    blurb: str = ""

    resources: tuple[Resource, ...] = ()
    raw: tuple[RawEndpoint, ...] = ()

    def tabs(self) -> list[Tab]:
        return [
            Tab("overview", "Overview", "overview"),
            *(Tab(r.key, r.label, f"resource:{r.key}") for r in self.resources),
            Tab("settings", "Settings", "settings"),
            Tab("raw", "Raw", "raw"),
        ]

    def resource(self, key: str) -> Resource | None:
        return next((r for r in self.resources if r.key == key), None)

    async def summary(self, ctx: SourceCtx) -> Summary:
        """Default: the connection check (``connections/checks.py``)."""
        from orchestrator.connections.checks import check_connection

        result = await check_connection(ctx.conn, ctx.settings)
        if result.ok is None:
            return Summary(state="unknown", label="not checked", detail=result.detail)
        return Summary(
            state="ok" if result.ok else "fail",
            label="reachable" if result.ok else "unreachable",
            detail=result.detail,
            ms=result.ms,
        )

    async def overview(self, ctx: SourceCtx) -> Overview:
        return Overview(facts=self.facts(ctx))

    def facts(self, ctx: SourceCtx) -> list[tuple[str, str]]:
        """The source's settings worth showing (never a secret)."""
        spec = ctx.conn.spec
        out: list[tuple[str, str]] = []
        for f in spec.fields:
            if f.is_secret:
                out.append((f.label, "set" if ctx.conn.has_secret(f) else "not set"))
                continue
            value = ctx.conn.value(f)
            if value not in ("", None):
                out.append(
                    (f.label, "yes" if value is True else "no" if value is False else str(value))
                )
        return out

    def address(self, conn: Connection) -> str:
        """Where it answers, for the header and lists (the marker field)."""
        value = conn.value(conn.spec.field(conn.spec.marker))
        return str(value or "")

    async def links(self, ctx: SourceCtx) -> list[dict[str, Any]]:
        """What this source's things are elsewhere. Empty by default."""
        return []


__all__ = [
    "Action",
    "Chip",
    "Column",
    "Connector",
    "Overview",
    "RawEndpoint",
    "Resource",
    "SourceCtx",
    "Summary",
    "Tab",
]
