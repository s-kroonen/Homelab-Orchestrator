"""A project's configuration as the setup wizard sees it: ``.env`` plus ``connections.yaml``.

The wizard asks for ``KEY=value`` answers. The adapter-related ones (Proxmox, PBS, Traefik Manager,
Portainer, MQTT, alerts, SSH...) are stored as connections, everything else stays in ``.env``.
Reading puts the two back together; saving takes them apart again, so an older ``.env`` that still
holds connector keys is moved into the file the next time anything is saved.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.connections import envmap
from orchestrator.connections.model import Connection, ConnectionError
from orchestrator.connections.store import ConnectionsFileError, read_file, write_file
from orchestrator.setup.envfile import EnvFileError, read_env, render_generated, write_env

FILE_NAME = "connections.yaml"


def connections_path(env_path: Path) -> Path:
    """Next to services.yaml, in the config directory the containers mount: the setup container
    can reach it, and so can you."""
    return env_path.parent / "config" / FILE_NAME


def _read(path: Path) -> list[Connection]:
    try:
        return read_file(path)
    except ConnectionsFileError as exc:
        raise EnvFileError(str(exc)) from exc


def read_values(env_path: Path, conns_path: Path | None = None) -> dict[str, str]:
    """``.env`` with the connections laid over it (the file wins for the keys it owns)."""
    conns_path = conns_path or connections_path(env_path)
    values = read_env(env_path)
    kinds = {c.kind for c in _read(conns_path)} if conns_path.is_file() else set()
    if kinds:
        from orchestrator.connections.kinds import BY_KIND

        for kind in kinds:  # a kind in the file replaces whatever .env still says about it
            for f in BY_KIND[kind].fields:
                values.pop(f.setting.upper(), None)
        values.update(envmap.to_values(_read(conns_path)))
    return values


def _merge(existing: list[Connection], new: list[Connection]) -> list[Connection]:
    out = list(existing)
    for conn in new:
        same = next((c for c in out if c.id == conn.id), None)
        if same is None:
            out.append(conn)
            continue
        # keep the name, site, scope and notes someone gave it
        same.settings, same.secrets = conn.settings, conn.secrets
        same.enabled = conn.enabled
    return out


def _ids_in(existing: list[Connection]) -> dict[str, str]:
    """Per kind, the connection the wizard's answers are about: the one in use."""
    ids: dict[str, str] = {}
    for c in existing:
        if c.kind not in ids or (c.enabled and c.default):
            ids[c.kind] = c.id
    return ids


def save_values(
    values: dict[str, str], env_path: Path, conns_path: Path | None = None
) -> list[Path]:
    """Write ``values``: connector keys into the connections file, the rest into ``.env``.
    Returns the backups of the files it replaced. Nothing is written if either part is invalid."""
    conns_path = conns_path or connections_path(env_path)
    render_generated(values)  # the .env part must render before anything moves
    existing = _read(conns_path) if conns_path.is_file() else []
    moved = envmap.from_values(values, _ids_in(existing))
    backups: list[Path] = []
    if moved:
        try:
            backup = write_file(conns_path, _merge(existing, moved))
        except ConnectionError as exc:
            raise EnvFileError(str(exc)) from exc
        if backup is not None:
            backups.append(backup)
    env_backup = write_env(env_path, values)
    if env_backup is not None:
        backups.append(env_backup)
    return backups
