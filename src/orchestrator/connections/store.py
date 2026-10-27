"""``connections.yaml``: reading it, writing it, and the connections it implies.

The file is owned by the dashboard (and ``orchestrator-cli connections``): it is written whole,
atomically, readable by the owner only, and the previous copy is kept. Connections that exist only
as ``.env`` keys (an install from before this file) are worked out on the fly and shown as coming
from ``.env`` until they are moved.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from ruamel.yaml import YAML

from orchestrator.config import Settings
from orchestrator.connections.kinds import BY_KIND, KINDS, KindSpec
from orchestrator.connections.model import Connection, ConnectionError

FILE_VERSION = 1
KEEP_BACKUPS = 5
_lock = threading.RLock()

_HEADER = (
    "# Connections: what this orchestrator talks to. Edited from the dashboard (Connections) or\n"
    "# `orchestrator-cli connections`; applied without a restart. Holds credentials: keep it\n"
    "# private (the file is written readable by its owner only).\n"
)


class ConnectionsFileError(Exception):
    """The file exists but cannot be used."""


def path_for(settings: Settings) -> Path:
    custom = getattr(settings, "connections_path", None)
    # next to services.yaml: that directory is the one every container mounts (the setup
    # container included), and the one you can see and back up from the host
    return Path(custom) if custom else Path(settings.services_yaml_path).parent / "connections.yaml"


# -- the file ------------------------------------------------------------------------------------


def read_file(path: Path) -> list[Connection]:
    """The connections in ``path`` ([] if it does not exist). Raises
    :class:`ConnectionsFileError` when it is there but unusable."""
    if not path.is_file():
        return []
    try:
        doc = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ConnectionsFileError(f"{path}: not valid YAML ({exc})") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("connections", []), list):
        raise ConnectionsFileError(f"{path}: expected a 'connections:' list")
    if int(doc.get("version", FILE_VERSION)) > FILE_VERSION:
        raise ConnectionsFileError(
            f"{path}: written by a newer orchestrator (version {doc['version']})"
        )
    out: list[Connection] = []
    for index, raw in enumerate(doc.get("connections") or []):
        try:
            conn = Connection.model_validate(raw)
            conn.validate_values()
        except Exception as exc:
            ident = raw.get("id") if isinstance(raw, dict) else index
            raise ConnectionsFileError(f"{path}: connection {ident}: {exc}") from exc
        out.append(conn)
    problems = check_set(out)
    if problems:
        raise ConnectionsFileError(f"{path}: {problems[0]}")
    return out


def dump_connections(conns: list[Connection]) -> str:
    import io

    y = YAML()
    y.default_flow_style = False
    y.indent(mapping=2, sequence=4, offset=2)
    doc: dict[str, Any] = {
        "version": FILE_VERSION,
        "connections": [
            {k: v for k, v in c.model_dump().items() if not (k in {"secrets", "notes"} and not v)}
            for c in conns
        ],
    }
    buf = io.StringIO()
    buf.write(_HEADER)
    y.dump(doc, buf)
    return buf.getvalue()


def write_file(path: Path, conns: list[Connection], *, keep: int = KEEP_BACKUPS) -> Path | None:
    """Write ``conns`` to ``path``: validated, atomic, mode 0600. Returns the backup of the file
    it replaced (None if there was none)."""
    for conn in conns:
        conn.validate_values()
    problems = check_set(conns)
    if problems:
        raise ConnectionError(problems[0])
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup: Path | None = None
        if path.is_file():
            folder = path.parent / "connections-backups"
            folder.mkdir(parents=True, exist_ok=True)
            backup = folder / f"{path.name}.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
            shutil.copy2(path, backup)
            with contextlib.suppress(OSError):
                backup.chmod(0o600)
            for old in sorted(folder.glob(f"{path.name}.*"))[:-keep]:
                with contextlib.suppress(OSError):
                    old.unlink()
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(dump_connections(conns))
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        return backup


# -- rules over the whole set ----------------------------------------------------------------------


def check_set(conns: list[Connection]) -> list[str]:
    """Problems with the set as a whole (empty = fine)."""
    problems: list[str] = []
    seen: set[str] = set()
    for c in conns:
        if c.id in seen:
            problems.append(f"two connections are called {c.id!r}")
        seen.add(c.id)
        if c.kind not in BY_KIND:
            problems.append(f"{c.id}: unknown kind {c.kind!r}")
    for spec in KINDS:
        mine = [c for c in conns if c.kind == spec.kind]
        on = [c for c in mine if c.enabled]
        if not spec.multi and len(on) > 1:
            problems.append(
                f"only one {spec.title} connection can be enabled at a time "
                f"({', '.join(c.id for c in on)}); disable the others"
            )
        if sum(1 for c in on if c.default) > 1:
            problems.append(f"more than one enabled {spec.title} connection is the default")
    return problems


def primary(conns: list[Connection], kind: str) -> Connection | None:
    """The connection of ``kind`` that is in use: the enabled default, else the first enabled."""
    on = [c for c in conns if c.kind == kind and c.enabled]
    return next((c for c in on if c.default), on[0] if on else None)


# -- .env-only connections ---------------------------------------------------------------------------


def values_of(settings: Settings, spec: KindSpec) -> tuple[dict[str, Any], dict[str, str]]:
    plain: dict[str, Any] = {}
    secret: dict[str, str] = {}
    for f in spec.fields:
        raw = getattr(settings, f.setting)
        if isinstance(raw, SecretStr):
            if raw.get_secret_value():
                secret[f.name] = raw.get_secret_value()
        elif raw != f.setting_default or f.name == spec.marker:
            plain[f.name] = raw
    return plain, secret


def env_connections(settings: Settings) -> list[Connection]:
    """The connections that exist only as environment / ``.env`` values (a kind counts when its
    marker field is set). ``settings`` must be the untouched environment view."""
    out: list[Connection] = []
    for spec in KINDS:
        marker = spec.field(spec.marker)
        raw = getattr(settings, marker.setting)
        text = raw.get_secret_value() if isinstance(raw, SecretStr) else str(raw or "")
        if not text.strip() or text == str(marker.setting_default):
            continue
        plain, secret = values_of(settings, spec)
        conn = Connection(
            id=spec.kind,
            kind=spec.kind,
            name=spec.title,
            default=True,
            settings=plain,
            secrets=secret,
        )
        conn.source = "env"
        out.append(conn)
    return out
