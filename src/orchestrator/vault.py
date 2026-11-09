"""Secrets at rest: one master key, AES-256-GCM, values stored as ``enc:v1:<key-id>:<nonce>:<ct>``.

Every credential the orchestrator keeps is encrypted with it (OR-70):

* the secret fields of every connection in ``connections.yaml`` (and its backup copies);
* the deploy webhook keys and the push (VAPID) private key in the database (``DB_SECRETS``);
* the session-cookie key is not stored at all: it is derived from the master key, unless
  ``SESSION_SECRET`` is set explicitly.

**The master key** lives outside the data it protects, 32 random bytes as base64 text:

1. the file ``ORCHESTRATOR_MASTER_KEY_FILE`` names, or
2. a Docker secret at ``/run/secrets/orchestrator_master_key``, or
3. ``STATE_DIR/keys/master.key``, made on first start (mode 0400, its folder 0700).

Only its fingerprint (the key id) is ever logged or shown. ``orchestrator-cli secrets
export-key`` prints the key itself once, for a password manager: without it, a backup restored
on another host cannot read its secrets.

**No key, encrypted data:** the database records the id of the key in use (``vault.key``). If
that key is missing or a different one is found, the orchestrator refuses to start rather than
run with blank credentials.

**Plain text from before** is read as it is and encrypted by :func:`encrypt_everything`, which
runs at start and from ``orchestrator-cli secrets encrypt``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import os
import secrets
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from orchestrator.config import Settings
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

PREFIX = "enc:v1:"
DOCKER_SECRET = Path("/run/secrets/orchestrator_master_key")
#: The ``setting`` row that records which key the data is encrypted with.
SETTING_KEY_ID = "vault.key"
#: ``setting`` rows holding secrets: (key, or key prefix ending in "."), and the JSON field.
DB_SECRETS: tuple[tuple[str, str], ...] = (
    ("deploy.webhook.", "secret"),
    ("push.vapid", "private_pem"),
)

_lock = threading.RLock()


class VaultError(Exception):
    """A secret cannot be read or written; the message says why and what to do."""


@dataclass(frozen=True)
class Key:
    raw: bytes
    path: Path

    @property
    def id(self) -> str:
        """The fingerprint: safe to log, show and store."""
        return hashlib.sha256(b"orchestrator-vault:" + self.raw).hexdigest()[:12]


@dataclass
class Vault:
    key: Key
    #: Older keys still readable (during a rotation), by id.
    previous: dict[str, Key] = field(default_factory=dict)

    # -- values -----------------------------------------------------------------------------

    def encrypt(self, text: str) -> str:
        if text == "" or is_encrypted(text):
            return text
        nonce = os.urandom(12)
        ct = AESGCM(self.key.raw).encrypt(nonce, text.encode("utf-8"), None)
        return f"{PREFIX}{self.key.id}:{_b64(nonce)}:{_b64(ct)}"

    def decrypt(self, value: str) -> str:
        if not is_encrypted(value):
            return value  # plain text from before encryption; encrypt_everything moves it
        try:
            kid, nonce, ct = value[len(PREFIX) :].split(":")
        except ValueError as exc:
            raise VaultError(
                "an encrypted value is damaged (it does not have three parts)"
            ) from exc
        key = self.key if kid == self.key.id else self.previous.get(kid)
        if key is None:
            raise VaultError(
                f"a secret was encrypted with key {kid}, but this orchestrator has key "
                f"{self.key.id}. Put the right master key back (see docs/secrets.md)."
            )
        try:
            return AESGCM(key.raw).decrypt(_unb64(nonce), _unb64(ct), None).decode("utf-8")
        except Exception as exc:
            raise VaultError(f"a secret encrypted with key {kid} cannot be decrypted") from exc

    def reencrypt(self, value: str) -> str:
        """``value`` under the current key (plain text is encrypted, other keys re-encrypted)."""
        if not value:
            return value
        if is_encrypted(value) and value[len(PREFIX) :].startswith(self.key.id + ":"):
            return value
        return self.encrypt(self.decrypt(value))

    # -- derived keys -------------------------------------------------------------------------

    def derive(self, purpose: str) -> str:
        """A stable secret for ``purpose`` (e.g. the session cookie), never stored anywhere."""
        return hmac.new(self.key.raw, f"derive:{purpose}".encode(), hashlib.sha256).hexdigest()


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# -- the key file ------------------------------------------------------------------------------


def key_path(settings: Settings) -> Path:
    explicit = (getattr(settings, "orchestrator_master_key_file", "") or "").strip()
    if explicit:
        return Path(explicit)
    if DOCKER_SECRET.is_file():
        return DOCKER_SECRET
    return Path(settings.state_dir) / "keys" / "master.key"


def _parse_key(text: bytes, path: Path) -> Key:
    stripped = text.strip()
    try:
        raw = base64.b64decode(stripped, validate=True) if len(stripped) != 32 else stripped
    except Exception:
        raw = b""
    if len(raw) != 32:
        raise VaultError(f"{path} is not a master key (expected 32 bytes, base64)")
    return Key(raw=raw, path=path)


def read_key(path: Path) -> Key | None:
    if not path.is_file():
        return None
    return _parse_key(path.read_bytes(), path)


def write_key(path: Path, raw: bytes) -> Key:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.b64encode(raw) + b"\n")
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        path.chmod(0o400)
    return Key(raw=raw, path=path)


def _previous_keys(path: Path) -> dict[str, Key]:
    """Keys kept during a rotation: ``master.key.previous`` next to the key."""
    out: dict[str, Key] = {}
    prev = path.with_name(path.name + ".previous")
    with contextlib.suppress(VaultError):
        key = read_key(prev)
        if key is not None:
            out[key.id] = key
    return out


# -- the vault in use ----------------------------------------------------------------------------

_current: Vault | None = None


def open_vault(settings: Settings, *, expected_id: str | None = None, create: bool = True) -> Vault:
    """The vault for ``settings``: its key read, or made when there is none and nothing says a
    key existed. ``expected_id`` is the key id the data was encrypted with (from the database)."""
    path = key_path(settings)
    key = read_key(path)
    if key is None:
        if expected_id:
            raise VaultError(
                f"the master key is missing: {path} does not exist, but the data here was "
                f"encrypted with key {expected_id}. Put that key back (from your password "
                "manager: orchestrator-cli secrets export-key printed it), or point "
                "ORCHESTRATOR_MASTER_KEY_FILE at it. The orchestrator will not start with "
                "unreadable credentials."
            )
        if not create:
            raise VaultError(f"there is no master key at {path}")
        key = write_key(path, secrets.token_bytes(32))
        log.warning(
            "vault.key_created",
            key_id=key.id,
            path=str(path),
            hint="Back it up now: orchestrator-cli secrets export-key. Without it, encrypted "
            "credentials and backups restored elsewhere cannot be read.",
        )
    vault = Vault(key=key, previous=_previous_keys(path))
    if expected_id and expected_id != key.id and expected_id not in vault.previous:
        raise VaultError(
            f"wrong master key: {path} is key {key.id}, but the data here was encrypted with "
            f"key {expected_id}. Put the right key back; nothing was changed."
        )
    return vault


def current(settings: Settings | None = None) -> Vault:
    """The vault in use, opened on first use (making a key if there is none yet)."""
    global _current
    with _lock:
        if _current is None:
            from orchestrator.config import get_settings

            _current = open_vault(settings or get_settings())
        return _current


def use(vault: Vault | None) -> None:
    """Set (or with None: forget) the vault in use. Tests and the start-up check use it."""
    global _current
    with _lock:
        _current = vault


def encrypt(text: str) -> str:
    return current().encrypt(text)


def decrypt(value: str) -> str:
    if not is_encrypted(value):
        return value  # plain text needs no key: no key is opened (or made) for it
    return current().decrypt(value)


def session_secret(settings: Settings) -> str:
    """The key that signs the session cookie: ``SESSION_SECRET`` when set explicitly, otherwise
    derived from the master key (nothing to store, nothing in ``.env``)."""
    explicit = settings.session_secret.get_secret_value()
    if explicit and explicit != "dev-only-change-me":
        return explicit
    return current(settings).derive("session-cookie")


# -- the database ------------------------------------------------------------------------------


def _db_secret_rows(session: Any) -> Iterable[tuple[Any, str]]:
    from sqlmodel import select

    from orchestrator.db.models import Setting

    for row in session.exec(select(Setting)).all():
        for key, fld in DB_SECRETS:
            hit = row.key.startswith(key) if key.endswith(".") else row.key == key
            if hit and (row.value or {}).get(fld):
                yield row, fld


def recorded_key_id(session: Any) -> str | None:
    from orchestrator.db.models import Setting

    row = session.get(Setting, SETTING_KEY_ID)
    return str((row.value or {}).get("id") or "") or None if row else None


def record_key_id(session: Any, key_id: str) -> None:
    from datetime import UTC, datetime

    from orchestrator.db.models import Setting

    row = session.get(Setting, SETTING_KEY_ID) or Setting(key=SETTING_KEY_ID)
    row.value = {"id": key_id}
    row.updated_at = datetime.now(UTC)
    session.add(row)


@dataclass
class Report:
    """What :func:`encrypt_everything` / :func:`status` found."""

    key_id: str = ""
    key_path: str = ""
    connections_encrypted: int = 0
    connections_plain: int = 0
    db_encrypted: int = 0
    db_plain: int = 0
    backups_rewritten: int = 0
    backups_unreadable: list[str] = field(default_factory=list)
    changed: bool = False

    @property
    def plain(self) -> int:
        return self.connections_plain + self.db_plain


def _count_connections(vault: Vault, settings: Settings, report: Report) -> None:
    from orchestrator.connections.store import ConnectionsFileError, path_for, raw_secrets

    try:
        values = raw_secrets(path_for(settings))
    except ConnectionsFileError:
        return
    for value in values:
        if is_encrypted(value):
            report.connections_encrypted += 1
        else:
            report.connections_plain += 1


def status(settings: Settings, session: Any) -> Report:
    vault = current(settings)
    report = Report(key_id=vault.key.id, key_path=str(vault.key.path))
    _count_connections(vault, settings, report)
    for row, fld in _db_secret_rows(session):
        if is_encrypted(row.value[fld]):
            report.db_encrypted += 1
        else:
            report.db_plain += 1
    return report


def encrypt_everything(settings: Settings, session: Any) -> Report:
    """Bring every stored secret under the current key: plain text from before is encrypted,
    values under an older key are re-encrypted, ``connections.yaml`` backups included. Then the
    database records the key id, and SQLite is vacuumed so no old copy survives in a free
    page. Safe to run any number of times."""
    from orchestrator.connections import store

    vault = current(settings)
    report = Report(key_id=vault.key.id, key_path=str(vault.key.path))

    path = store.path_for(settings)
    # Under the store's lock: a dashboard edit cannot land between the read and the write.
    with store._lock, contextlib.suppress(store.ConnectionsFileError):
        values = store.raw_secrets(path)
        stale = [v for v in values if not v.startswith(f"{PREFIX}{vault.key.id}:")]
        if stale:
            conns = store.read_file(path)
            store.write_file(path, conns, keep_backup=False)
            report.connections_encrypted = len(values)
            report.changed = True
    folder = path.parent / "connections-backups"
    for backup in sorted(folder.glob(f"{path.name}.*")) if folder.is_dir() else []:
        try:
            values = store.raw_secrets(backup)
            if any(not v.startswith(f"{PREFIX}{vault.key.id}:") for v in values):
                store.write_file(backup, store.read_file(backup), keep_backup=False)
                report.backups_rewritten += 1
        except (store.ConnectionsFileError, VaultError, ValueError) as exc:
            report.backups_unreadable.append(f"{backup.name}: {exc}")

    for row, fld in _db_secret_rows(session):
        old = str(row.value[fld])
        new = vault.reencrypt(old)
        if new != old:
            row.value = {**row.value, fld: new}
            session.add(row)
            report.db_plain += 0 if is_encrypted(old) else 1
            report.changed = True
        report.db_encrypted += 1
    if recorded_key_id(session) != vault.key.id:
        record_key_id(session, vault.key.id)
        report.changed = True
    session.commit()
    if report.changed or _vacuum_pending(session):
        _vacuum(session)
    return report


SETTING_VACUUM = "vault.vacuum_pending"


def _vacuum_pending(session: Any) -> bool:
    from orchestrator.db.models import Setting

    return session.get(Setting, SETTING_VACUUM) is not None


def _vacuum(session: Any, attempts: int = 5) -> None:
    """Rewrite the SQLite file so the plain-text values just replaced leave no trace in a free
    page. A busy database is retried; if it stays busy, a flag makes the next start do it."""
    import time

    from sqlalchemy import text

    from orchestrator.db.models import Setting

    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        return
    if session.get(Setting, SETTING_VACUUM) is None:
        session.add(Setting(key=SETTING_VACUUM, value={}))
        session.commit()
    for attempt in range(attempts):
        try:
            with bind.connect() as conn:
                conn.execute(text("DELETE FROM setting WHERE key = :k"), {"k": SETTING_VACUUM})
                conn.commit()
                conn.execution_options(isolation_level="AUTOCOMMIT").execute(text("VACUUM"))
            session.expire_all()
            return
        except Exception as exc:
            if attempt == attempts - 1:
                with contextlib.suppress(Exception):
                    session.merge(Setting(key=SETTING_VACUUM, value={}))
                    session.commit()
                log.warning("vault.vacuum_skipped", error=str(exc), hint="the next start retries")
                return
            time.sleep(2)


def boot(settings: Settings) -> Vault:
    """Open the vault for this process, checked against the key id the database recorded.
    Raises :class:`VaultError` for a missing or wrong key: the caller must not go on."""
    from orchestrator.db.session import build_engine, session_scope

    expected: str | None = None
    with contextlib.suppress(Exception):  # no database or no table yet: nothing to check
        build_engine(settings)
        with session_scope() as session:
            expected = recorded_key_id(session)
    vault = open_vault(settings, expected_id=expected)
    use(vault)
    if expected is None:  # from now on a missing key is noticed, even before anything is migrated
        with contextlib.suppress(Exception), session_scope() as session:
            record_key_id(session, vault.key.id)
    return vault


def startup(settings: Settings) -> Report:
    """At start, once the database is migrated: open the vault (refusing a missing or wrong
    key), then encrypt what is still plain. Raises :class:`VaultError` when the orchestrator
    must not run."""
    from orchestrator.db.session import session_scope

    boot(settings)
    with session_scope() as session:
        report = encrypt_everything(settings, session)
    if report.changed:
        log.info(
            "vault.encrypted",
            key_id=report.key_id,
            connections=report.connections_encrypted,
            database=report.db_encrypted,
            backups=report.backups_rewritten,
        )
    for problem in report.backups_unreadable:
        log.warning("vault.backup_unreadable", detail=problem)
    return report


def decrypt_everything(settings: Settings, session: Any) -> int:
    """Write every stored secret back as plain text: before going back to a version from
    before encryption, which would read ``enc:v1:...`` as the credential itself. Returns how
    many values it changed. The key stays where it is."""
    from orchestrator.connections import store
    from orchestrator.db.models import Setting

    current(settings)
    changed = 0
    path = store.path_for(settings)
    folder = path.parent / "connections-backups"
    files = [path, *(sorted(folder.glob(f"{path.name}.*")) if folder.is_dir() else [])]
    for f in files:
        with contextlib.suppress(store.ConnectionsFileError):
            values = store.raw_secrets(f)
            if any(is_encrypted(v) for v in values):
                store.write_file(f, store.read_file(f), keep_backup=False, encrypt=False)
                changed += sum(1 for v in values if is_encrypted(v))
    for row, fld in list(_db_secret_rows(session)):
        if is_encrypted(row.value[fld]):
            row.value = {**row.value, fld: decrypt(row.value[fld])}
            session.add(row)
            changed += 1
    marker = session.get(Setting, SETTING_KEY_ID)
    if marker is not None:
        session.delete(marker)
    session.commit()
    return changed


def rotate(settings: Settings, session: Any) -> tuple[str, str]:
    """A new master key, everything re-encrypted with it, then the old one retired. Returns
    (old id, new id). The key file must be writable (not a read-only Docker secret)."""
    old = current(settings)
    path = old.key.path
    if not os.access(path.parent, os.W_OK):
        raise VaultError(
            f"{path} is read-only (a Docker secret?): make a new key with `openssl rand "
            "-base64 32`, keep the old one as ORCHESTRATOR_MASTER_KEY_FILE.previous, then "
            "restart"
        )
    previous = path.with_name(path.name + ".previous")
    write_key(previous, old.key.raw)
    new = write_key(path, secrets.token_bytes(32))
    vault = Vault(key=new, previous={old.key.id: old.key})
    use(vault)
    encrypt_everything(settings, session)
    with contextlib.suppress(OSError):
        previous.chmod(0o600)
        previous.unlink()
    use(Vault(key=new))
    log.warning("vault.key_rotated", old=old.key.id, new=new.id)
    return old.key.id, new.id


__all__ = [
    "DB_SECRETS",
    "PREFIX",
    "Key",
    "Report",
    "Vault",
    "VaultError",
    "boot",
    "current",
    "decrypt",
    "decrypt_everything",
    "encrypt",
    "encrypt_everything",
    "is_encrypted",
    "key_path",
    "open_vault",
    "rotate",
    "session_secret",
    "startup",
    "status",
    "use",
]
