"""Putting connections to work: onto :class:`Settings`, and into per-instance settings.

Two jobs.

* :func:`apply_to` fills the legacy ``Settings`` attributes (``portainer_url``, ``proxmox_host``...)
  from the connection of each kind that is in use, so everything written against ``settings.*``
  keeps working. A kind that appears in the file is owned by the file (its ``.env`` keys are
  ignored); a kind that does not is left as the environment has it.
* :func:`settings_for` gives a copy of the settings with one particular connection's values: what
  a second Portainer's adapter, or a controller that names an instance, is built from.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from orchestrator.config import Settings
from orchestrator.connections.kinds import BY_KIND, KINDS, KindSpec
from orchestrator.connections.model import Connection
from orchestrator.connections.store import (
    ConnectionsFileError,
    env_connections,
    path_for,
    primary,
    read_file,
)
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)
_lock = threading.RLock()


@dataclass
class Snapshot:
    """What was in force the last time connections were applied."""

    path: Path | None = None
    #: The file's own connections.
    file: list[Connection] = field(default_factory=list)
    #: Connections that only exist in ``.env`` (for kinds the file does not mention).
    env: list[Connection] = field(default_factory=list)
    #: Why the file could not be used ("" = fine). Then the environment is used as it is.
    error: str = ""

    @property
    def all(self) -> list[Connection]:
        return [*self.file, *self.env]

    def of(self, kind: str, *, enabled_only: bool = False) -> list[Connection]:
        return [c for c in self.all if c.kind == kind and (c.enabled or not enabled_only)]

    def get(self, ident: str) -> Connection | None:
        return next((c for c in self.all if c.id == ident), None)

    def primary(self, kind: str) -> Connection | None:
        return primary(self.all, kind)


_snapshot = Snapshot()


def current() -> Snapshot:
    return _snapshot


def _setting_value(spec_field: Any, conn: Connection | None) -> Any:
    value = spec_field.default if conn is None else conn.value(spec_field)
    return SecretStr(str(value)) if spec_field.is_secret else value


def values_to_settings(spec: KindSpec, conn: Connection | None) -> dict[str, Any]:
    """The ``Settings`` attributes ``conn`` sets (``None`` = the kind is switched off: defaults)."""
    out = {f.setting: _setting_value(f, conn) for f in spec.fields}
    if spec.selector is not None:
        attr, live = spec.selector
        out[attr] = live if conn is not None else "dry_run"
    return out


def apply_to(settings: Settings, *, path: Path | None = None) -> Snapshot:
    """Read the file and write what it says onto ``settings`` (in place, so holders of the object
    see it). Never raises: a broken file leaves the environment in force and is reported."""
    global _snapshot
    with _lock:
        path = path or path_for(settings)
        snap = Snapshot(path=path)
        try:
            snap.file = read_file(path)
        except ConnectionsFileError as exc:
            snap.error = str(exc)
            log.error("connections.file_unusable", path=str(path), error=str(exc))
        in_file = {c.kind for c in snap.file}
        snap.env = [c for c in env_connections(settings) if c.kind not in in_file]
        for spec in KINDS:
            if spec.kind not in in_file:
                continue
            conn = primary(snap.file, spec.kind)
            for attr, value in values_to_settings(spec, conn).items():
                setattr(settings, attr, value)
        channels = {s.kind for s in KINDS if s.channel}
        mine = [c for c in snap.file if c.kind in channels]
        if mine:  # the file decides whether alerts go anywhere; the channels filled in above
            settings.notifier_adapter = (
                "auto" if any(c.enabled and c.configured for c in mine) else "null"
            )
        _snapshot = snap
        return snap


def settings_for(settings: Settings, conn: Connection) -> Settings:
    """A copy of ``settings`` with ``conn``'s values: one instance's view of the world."""
    return settings.model_copy(update=values_to_settings(BY_KIND[conn.kind], conn))
