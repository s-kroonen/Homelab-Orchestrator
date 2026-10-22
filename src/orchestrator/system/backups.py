"""Backups of the orchestrator's own state: the SQLite database and services.yaml.

A backup is one ``orchestrator-<time>-<reason>.tar.gz`` in ``state_dir/backups``
holding ``manifest.json``, ``orchestrator.db`` (a consistent copy taken with
SQLite's online backup, safe while the app is running) and ``services.yaml``.
``.env`` is not included: it lives on the host and holds secrets.

Restoring never swaps the database under a running app. It is *staged*
(extracted next to the live files with a marker) and applied on the next start,
before the engine opens, after a safety backup of what is being replaced.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator import __version__
from orchestrator.config import Settings
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

DB_MEMBER = "orchestrator.db"
REGISTRY_MEMBER = "services.yaml"
MANIFEST_MEMBER = "manifest.json"
_NAME_RE = re.compile(r"^orchestrator-\d{8}-\d{6}(?:-[a-z0-9-]{1,40})?\.tar\.gz$")


class BackupError(Exception):
    """A backup/restore step could not be done (message is shown to the operator)."""


@dataclass(frozen=True)
class BackupInfo:
    name: str
    created_at: str
    size_bytes: int
    reason: str = ""
    note: str = ""
    version: str = ""
    git_sha: str = ""
    schema_revision: str = ""
    has_registry: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def sqlite_path(settings: Settings) -> Path:
    """The database file, or BackupError when the database is not SQLite."""
    url = settings.database_url
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise BackupError("self-backup supports the SQLite database only")
    raw = url[len(prefix) :]
    if raw in {"", ":memory:"}:
        raise BackupError("the database is in memory - nothing to back up")
    return Path(raw)


def backup_dir(settings: Settings) -> Path:
    path = Path(settings.state_dir) / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def backup_path(settings: Settings, name: str) -> Path:
    if not valid_name(name):
        raise BackupError(f"{name!r} is not a backup file name")
    path = backup_dir(settings) / name
    if not path.is_file():
        raise BackupError(f"no backup named {name!r}")
    return path


def _schema_revision(db: Path) -> str:
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""


def create_backup(settings: Settings, *, reason: str = "manual", note: str = "") -> BackupInfo:
    """Write a backup now. Safe while the app runs."""
    db = sqlite_path(settings)
    if not db.is_file():
        raise BackupError(f"database file {db} does not exist yet")
    now = datetime.now(UTC)
    name = f"orchestrator-{now:%Y%m%d-%H%M%S}-{_slug(reason) or 'manual'}.tar.gz"
    target = backup_dir(settings) / name
    n = 1
    while target.exists():  # two backups in one second
        n += 1
        target = backup_dir(settings) / name.replace(".tar.gz", f"-{n}.tar.gz")
    registry = Path(settings.services_yaml_path)

    with tempfile.TemporaryDirectory(prefix="orch-backup-") as tmp:
        snapshot = Path(tmp) / DB_MEMBER
        with (
            contextlib.closing(sqlite3.connect(db)) as src,
            contextlib.closing(sqlite3.connect(snapshot)) as dst,
        ):
            src.backup(dst)
        manifest = {
            "created_at": now.isoformat(),
            "reason": reason,
            "note": note,
            "version": __version__,
            "git_sha": settings.orchestrator_git_sha,
            "schema_revision": _schema_revision(snapshot),
            "has_registry": registry.is_file(),
            "sha256": {DB_MEMBER: _sha256(snapshot)},
        }
        partial = target.with_suffix(".partial")
        with tarfile.open(partial, "w:gz") as tar:
            data = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo(MANIFEST_MEMBER)
            info.size, info.mtime = len(data), time.time()
            tar.addfile(info, io.BytesIO(data))
            tar.add(snapshot, arcname=DB_MEMBER)
            if registry.is_file():
                tar.add(registry, arcname=REGISTRY_MEMBER)
        os.replace(partial, target)
    log.info("system.backup.created", name=target.name, reason=reason)
    return _describe(target)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest(path: Path) -> dict[str, Any]:
    try:
        with tarfile.open(path, "r:gz") as tar:
            member = tar.extractfile(MANIFEST_MEMBER)
            return json.loads(member.read()) if member else {}
    except (tarfile.TarError, OSError, ValueError, KeyError):
        return {}


def _describe(path: Path) -> BackupInfo:
    m = _manifest(path)
    return BackupInfo(
        name=path.name,
        created_at=str(
            m.get("created_at") or datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
        ),
        size_bytes=path.stat().st_size,
        reason=str(m.get("reason", "")),
        note=str(m.get("note", "")),
        version=str(m.get("version", "")),
        git_sha=str(m.get("git_sha", "")),
        schema_revision=str(m.get("schema_revision", "")),
        has_registry=bool(m.get("has_registry")),
    )


def list_backups(settings: Settings) -> list[BackupInfo]:
    paths = [p for p in backup_dir(settings).iterdir() if p.is_file() and valid_name(p.name)]
    return sorted((_describe(p) for p in paths), key=lambda b: b.created_at, reverse=True)


def delete_backup(settings: Settings, name: str) -> None:
    backup_path(settings, name).unlink()
    log.info("system.backup.deleted", name=name)


def prune_backups(settings: Settings, keep: int | None = None) -> list[str]:
    """Delete the oldest *scheduled* backups beyond ``keep``. Manual, pre-update,
    pre-migration and pre-restore backups are never pruned automatically."""
    limit = settings.self_backup_keep if keep is None else keep
    scheduled = [b for b in list_backups(settings) if b.reason == "scheduled"]
    gone: list[str] = []
    for b in scheduled[max(limit, 1) :]:
        with contextlib.suppress(BackupError, OSError):
            delete_backup(settings, b.name)
            gone.append(b.name)
    return gone


# -- restore ----------------------------------------------------------------


def _pending_dir(settings: Settings) -> Path:
    return Path(settings.state_dir) / "restore-pending"


def pending_restore(settings: Settings) -> dict[str, Any] | None:
    marker = _pending_dir(settings) / "pending.json"
    try:
        return json.loads(marker.read_text())
    except (OSError, ValueError):
        return None


def stage_restore(settings: Settings, name: str, *, known_revisions: set[str]) -> dict[str, Any]:
    """Prepare restoring ``name`` on the next start. Nothing live is touched."""
    path = backup_path(settings, name)
    manifest = _manifest(path)
    revision = str(manifest.get("schema_revision", ""))
    if revision and known_revisions and revision not in known_revisions:
        raise BackupError(
            f"this backup is from a newer schema ({revision}) than this version knows; "
            "update first, then restore it"
        )
    staging = _pending_dir(settings)
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with tarfile.open(path, "r:gz") as tar:
            for member in (DB_MEMBER, REGISTRY_MEMBER):
                try:
                    src = tar.extractfile(member)
                except KeyError:
                    src = None
                if src is None:
                    if member == DB_MEMBER:
                        raise BackupError("the backup has no database in it")
                    continue
                with (staging / member).open("wb") as out:
                    shutil.copyfileobj(src, out)
        expected = (manifest.get("sha256") or {}).get(DB_MEMBER)
        if expected and _sha256(staging / DB_MEMBER) != expected:
            raise BackupError("the backup's database failed its checksum - it is damaged")
    except (tarfile.TarError, OSError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise BackupError(f"could not read the backup: {exc}") from exc
    except BackupError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    marker = {
        "backup": name,
        "staged_at": datetime.now(UTC).isoformat(),
        "has_registry": (staging / REGISTRY_MEMBER).is_file(),
    }
    (staging / "pending.json").write_text(json.dumps(marker))
    return marker


def cancel_restore(settings: Settings) -> bool:
    staging = _pending_dir(settings)
    existed = staging.exists()
    shutil.rmtree(staging, ignore_errors=True)
    return existed


def apply_pending_restore(settings: Settings) -> str | None:
    """Called at startup BEFORE the database is opened. Replaces the live files
    with the staged ones, after a safety backup of the current state. Returns the
    restored backup's name, or None when nothing was pending."""
    marker = pending_restore(settings)
    if marker is None:
        return None
    staging = _pending_dir(settings)
    db = sqlite_path(settings)
    try:
        if db.is_file():
            with contextlib.suppress(BackupError):
                create_backup(settings, reason="pre-restore")
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                Path(str(db) + suffix).unlink()
        db.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging / DB_MEMBER), db)
        staged_registry = staging / REGISTRY_MEMBER
        if staged_registry.is_file():
            registry = Path(settings.services_yaml_path)
            registry.parent.mkdir(parents=True, exist_ok=True)
            if registry.is_file():
                stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
                shutil.copy2(registry, registry.with_name(f"{registry.name}.bak-{stamp}"))
            tmp = registry.with_name(registry.name + ".tmp")
            shutil.copy2(staged_registry, tmp)
            os.replace(tmp, registry)
    except OSError as exc:
        log.error("system.restore.failed", error=str(exc), backup=marker.get("backup"))
        return None
    shutil.rmtree(staging, ignore_errors=True)
    log.warning("system.restore.applied", backup=marker.get("backup"))
    return str(marker.get("backup"))
