"""Changing connections: the operations behind the Connections page and ``orchestrator-cli
connections``. Everything that writes goes through here so the rules are the same on both.

A write always starts from the file's own connections plus, for the kind being changed, any that
only exist in ``.env`` - otherwise adding a second Portainer would make the first (which was only
in ``.env``) disappear, because a kind that appears in the file belongs to the file.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from sqlmodel import Session, select

from orchestrator.config import Settings
from orchestrator.connections.kinds import KINDS, kind_spec
from orchestrator.connections.model import Connection, ConnectionError, Scope
from orchestrator.connections.store import (
    ConnectionsFileError,
    check_set,
    dump_connections,
    env_connections,
    path_for,
    primary,
    read_file,
    write_file,
)
from orchestrator.db.models import Probe, Service


class Refused(Exception):
    """The change is not allowed as things stand; ``status`` is the HTTP code to use."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Change:
    connection: Connection | None
    #: Services whose implicit links were pinned to a connection (see set_default).
    pinned: list[str]
    #: The registry was edited in memory and should be saved to services.yaml.
    registry_dirty: bool = False


def _pristine_env() -> Settings:
    return Settings()  # the environment as it is, without the file laid over it


def _load(settings: Settings, kind: str | None = None) -> tuple[Path, list[Connection]]:
    """The file's connections, plus those ``.env`` has for ``kind`` when the file has none."""
    path = path_for(settings)
    try:
        conns = read_file(path)
    except ConnectionsFileError as exc:
        raise Refused(f"the connections file cannot be used: {exc}", 409) from exc
    if kind is not None and kind not in {c.kind for c in conns}:
        mine = [c for c in env_connections(_pristine_env()) if c.kind == kind]
        for c in mine:
            c.source = "file"
        conns = [*conns, *mine]
    return path, conns


def _find(settings: Settings, ident: str) -> tuple[Path, list[Connection], Connection]:
    path, conns = _load(settings)
    conn = next((c for c in conns if c.id == ident), None)
    if conn is None:  # maybe it only exists in .env: bring its kind into the file first
        for env in env_connections(_pristine_env()):
            if env.id == ident:
                path, conns = _load(settings, env.kind)
                conn = next((c for c in conns if c.id == ident), None)
    if conn is None:
        raise Refused(f"no connection called {ident!r}", 404)
    return path, conns, conn


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", text.strip().lower()).strip("-_")[:40]


def _free_id(conns: list[Connection], wanted: str) -> str:
    taken = {c.id for c in conns}
    ident, n = wanted, 2
    while ident in taken:
        ident = f"{wanted}-{n}"
        n += 1
    return ident


def _apply_values(
    conn: Connection,
    settings_in: dict[str, Any] | None,
    secrets_in: dict[str, str] | None,
    clear_secrets: list[str] | None,
) -> None:
    """Merge form values onto ``conn``: a secret left blank keeps what is stored."""
    spec = conn.spec
    for key, value in (settings_in or {}).items():
        conn.settings[key] = value
    for key, value in (secrets_in or {}).items():
        if value not in (None, ""):
            conn.secrets[key] = str(value)
    for key in clear_secrets or []:
        conn.secrets.pop(key, None)
    for key in list(conn.settings):  # a blank non-text value falls back to the default
        field = next((f for f in spec.fields if f.name == key), None)
        if field is not None and conn.settings[key] in ("", None) and field.type != "text":
            conn.settings.pop(key)


def create(
    settings: Settings,
    *,
    kind: str,
    ident: str = "",
    name: str = "",
    enabled: bool = True,
    default: bool = False,
    site: str = "",
    scope: Scope = "local",
    notes: str = "",
    settings_in: dict[str, Any] | None = None,
    secrets_in: dict[str, str] | None = None,
) -> Change:
    try:
        spec = kind_spec(kind)
    except ValueError as exc:
        raise Refused(str(exc), 422) from exc
    path, conns = _load(settings, kind)
    ident = ident.strip() or _free_id(conns, slugify(name) or kind)
    if any(c.id == ident for c in conns):
        raise Refused(f"there is already a connection called {ident!r}")
    conn = Connection(
        id=ident,
        kind=kind,
        name=name or spec.title,
        enabled=enabled,
        default=default,
        site=site,
        scope=scope,
        notes=notes,
    )
    _apply_values(conn, settings_in, secrets_in, None)
    if default:
        for other in conns:
            if other.kind == kind:
                other.default = False
    try:
        conn.validate_values()
        write_file(path, [*conns, conn])
    except ConnectionError as exc:
        raise Refused(str(exc), 422) from exc
    return Change(conn, [])


def update(
    settings: Settings,
    ident: str,
    *,
    name: str | None = None,
    enabled: bool | None = None,
    site: str | None = None,
    scope: Scope | None = None,
    notes: str | None = None,
    settings_in: dict[str, Any] | None = None,
    secrets_in: dict[str, str] | None = None,
    clear_secrets: list[str] | None = None,
) -> Change:
    path, conns, conn = _find(settings, ident)
    if name is not None:
        conn.name = name
    if enabled is not None:
        conn.enabled = enabled
    if site is not None:
        conn.site = site
    if scope is not None:
        conn.scope = scope
    if notes is not None:
        conn.notes = notes
    _apply_values(conn, settings_in, secrets_in, clear_secrets)
    try:
        conn.validate_values()
        write_file(path, conns)
    except ConnectionError as exc:
        raise Refused(str(exc), 422) from exc
    return Change(conn, [])


def references(session: Session, settings: Settings, conn: Connection) -> list[str]:
    """Slugs of services whose controller or probes use this Portainer connection (a link that
    names none counts for the default one)."""
    if conn.kind != "portainer":
        return []
    from orchestrator.connections import runtime

    default = runtime.default_id(settings, "portainer")
    out: set[str] = set()
    for svc in session.exec(select(Service)).all():
        cfg = svc.controller or {}
        if (
            cfg.get("type") == "portainer"
            and (str(cfg.get("instance") or "") or default) == conn.id
        ):
            out.add(svc.slug)
    rows = session.exec(select(Probe, Service).where(Probe.service_id == Service.id)).all()
    for probe, svc in rows:
        cfg = probe.config or {}
        if (
            probe.kind.value == "portainer"
            and (str(cfg.get("instance") or "") or default) == conn.id
        ):
            out.add(svc.slug)
    return sorted(out)


def delete(session: Session, settings: Settings, ident: str, *, force: bool = False) -> Change:
    path, conns, conn = _find(settings, ident)
    used = references(session, settings, conn)
    if used and not force:
        raise Refused(
            f"{', '.join(used)} use(s) this connection - move or unlink them first "
            "(or delete it anyway with force)"
        )
    rest = [c for c in conns if c.id != ident]
    if conn.default:
        nxt = next((c for c in rest if c.kind == conn.kind and c.enabled), None)
        if nxt is not None:
            nxt.default = True
    try:
        write_file(path, rest)
    except ConnectionError as exc:
        raise Refused(str(exc), 422) from exc
    return Change(conn, [])


def pin_links(session: Session, kind: str, old_default: str) -> list[str]:
    """Make the Portainer links that name no connection name ``old_default``: they have meant
    "the default" so far, and must not follow it when it moves."""
    if kind != "portainer" or not old_default:
        return []
    pinned: set[str] = set()
    for svc in session.exec(select(Service)).all():
        cfg = dict(svc.controller or {})
        if cfg.get("type") == "portainer" and not cfg.get("instance"):
            cfg["instance"] = old_default
            svc.controller = cfg
            session.add(svc)
            pinned.add(svc.slug)
    rows = session.exec(select(Probe, Service).where(Probe.service_id == Service.id)).all()
    for probe, svc in rows:
        cfg = dict(probe.config or {})
        if probe.kind.value == "portainer" and not cfg.get("instance"):
            cfg["instance"] = old_default
            probe.config = cfg
            session.add(probe)
            pinned.add(svc.slug)
    return sorted(pinned)


def set_default(session: Session, settings: Settings, ident: str) -> Change:
    path, conns, conn = _find(settings, ident)
    if not conn.enabled:
        raise Refused("a connection that is switched off cannot be the default")
    current = primary(conns, conn.kind)
    pinned: list[str] = []
    if current is not None and current.id != conn.id:
        pinned = pin_links(session, conn.kind, current.id)
    for other in conns:
        if other.kind == conn.kind:
            other.default = other.id == conn.id
    write_file(path, conns)
    return Change(conn, pinned, registry_dirty=bool(pinned))


def import_env(settings: Settings) -> tuple[list[Connection], list[str]]:
    """Write every connection that only exists in ``.env`` into the file. Returns them and the
    environment keys that can now be deleted from ``.env``."""
    path, conns = _load(settings)
    in_file = {c.kind for c in conns}
    moved = [c for c in env_connections(_pristine_env()) if c.kind not in in_file]
    if not moved:
        return [], []
    for c in moved:
        c.source = "file"
    write_file(path, [*conns, *moved])
    keys = sorted(
        {f.setting.upper() for c in moved for f in c.spec.fields}
        | {s[0].upper() for c in moved if (s := c.spec.selector) is not None}
    )
    return moved, keys


def env_keys_in_use(settings: Settings) -> list[str]:
    """``.env`` keys that are set but no longer used because the file owns their kind."""
    from orchestrator.connections import runtime

    in_file = {c.kind for c in runtime.current().file}
    env = _pristine_env()
    out: set[str] = set()
    for spec in KINDS:
        if spec.kind not in in_file:
            continue
        for f in spec.fields:
            raw = getattr(env, f.setting)
            value = raw.get_secret_value() if isinstance(raw, SecretStr) else raw
            if value not in ("", None) and value != f.setting_default:
                out.add(f.setting.upper())
    return sorted(out)


# -- sharing between orchestrators -----------------------------------------------------------------


def export_shared(settings: Settings, *, with_secrets: bool = False, scope: str = "shared") -> str:
    """The connections meant for another orchestrator (``scope: shared``) as a file body. Secrets
    are left out unless asked for."""
    _path, conns = _load(settings)
    out = [c.model_copy(deep=True) for c in conns if scope == "all" or c.scope == scope]
    if not with_secrets:
        for c in out:
            c.secrets = {}
            c.enabled = False  # the receiver adds the secrets, then switches each on
    return dump_connections(out)


def import_shared(settings: Settings, body: str) -> tuple[list[str], list[str]]:
    """Merge a bundle made by :func:`export_shared`: new ids are added, ids already here are left
    as they are (their values may differ on purpose). Returns (added, skipped)."""
    path, conns = _load(settings)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(body)
        tmp = Path(fh.name)
    try:
        incoming = read_file(tmp)
    except ConnectionsFileError as exc:
        raise Refused(str(exc), 422) from exc
    finally:
        tmp.unlink(missing_ok=True)
    have = {c.id for c in conns}
    added: list[str] = []
    skipped: list[str] = []
    for c in incoming:
        if c.id in have:
            skipped.append(c.id)
            continue
        c.scope = "shared"
        conns.append(c)
        added.append(c.id)
    problems = check_set(conns)
    if problems:
        raise Refused(problems[0], 422)
    write_file(path, conns)
    return added, skipped
