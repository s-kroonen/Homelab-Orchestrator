"""A service's ``dump:`` block, and the command it runs inside the container.

Credentials are never read here: the command is a ``sh -c`` script that uses the environment
the container already has (``POSTGRES_USER`` / ``POSTGRES_PASSWORD``, ``MYSQL_ROOT_PASSWORD`` ...),
so nothing secret passes through the orchestrator or its logs. Names that go into a script are
restricted to a safe alphabet; only the ``command`` type runs text exactly as written, and that
text is the operator's own.
"""

from __future__ import annotations

import copy
import re
from typing import Any

TYPES = ("postgres", "mysql", "mariadb", "mongo", "sqlite", "command")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "type": "postgres",
    "container": "",
    "database": "",
    "user": "",
    "path": "",
    "command": "",
    "schedule": "",
    "keep": 7,
    "keep_days": 0,
    "timeout_s": 1800,
    "min_bytes": 1,
}

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./@+-]{1,200}$")

#: What the stored file is called, by type.
EXTENSION = {
    "postgres": "sql.gz",
    "mysql": "sql.gz",
    "mariadb": "sql.gz",
    "sqlite": "sql.gz",
    "mongo": "archive.gz",
    "command": "dump.gz",
}


def _bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"dump {what} must be true or false")
    return value


def _int(value: Any, what: str, low: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"dump {what} must be a whole number")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"dump {what} must be a whole number") from None
    if number < low:
        raise ValueError(f"dump {what} must be at least {low}")
    return number


def _text(value: Any, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"dump {what} must be text")
    return value.strip()


def normalize_dump(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate a ``dump:`` block and fill in the defaults. ``{}`` stays ``{}``."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("dump block must be an object")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ValueError(
            f"unknown dump key(s): {', '.join(sorted(unknown))} (known: {', '.join(DEFAULTS)})"
        )
    out = copy.deepcopy(DEFAULTS)
    out["enabled"] = _bool(raw.get("enabled", True), "'enabled'")
    kind = _text(raw.get("type", "postgres"), "type").lower()
    if kind not in TYPES:
        raise ValueError(f"dump type must be one of {', '.join(TYPES)}")
    out["type"] = kind
    out["container"] = _text(raw.get("container"), "container")
    for key in ("database", "user"):
        value = _text(raw.get(key), key)
        if value and not _SAFE_NAME.match(value):
            raise ValueError(f"dump {key} may only contain letters, digits and _ . @ - (max 64)")
        out[key] = value
    path = _text(raw.get("path"), "path")
    if path and not _SAFE_PATH.match(path):
        raise ValueError("dump path must be an absolute path inside the container")
    out["path"] = path
    out["command"] = _text(raw.get("command"), "command")
    out["schedule"] = _text(raw.get("schedule"), "schedule")
    if out["schedule"]:
        _check_cron(out["schedule"])
    out["keep"] = _int(raw.get("keep", 7), "keep", 1)
    out["keep_days"] = _int(raw.get("keep_days", 0), "keep_days", 0)
    out["timeout_s"] = _int(raw.get("timeout_s", 1800), "timeout_s", 10)
    out["min_bytes"] = _int(raw.get("min_bytes", 1), "min_bytes", 1)

    if kind == "sqlite" and not out["path"]:
        raise ValueError("a sqlite dump needs path (the database file inside the container)")
    if kind == "command" and not out["command"]:
        raise ValueError("a command dump needs command (its stdout is the dump)")
    if kind != "command" and out["command"]:
        raise ValueError("command is only for type: command")
    if kind != "sqlite" and out["path"]:
        raise ValueError("path is only for type: sqlite")
    if kind == "sqlite" and (out["database"] or out["user"]):
        raise ValueError("a sqlite dump takes path, not database / user")
    return out


def _check_cron(expr: str) -> None:
    from apscheduler.triggers.cron import CronTrigger

    try:
        CronTrigger.from_crontab(expr)
    except ValueError as exc:
        raise ValueError(f"dump schedule is not a valid cron expression: {exc}") from None


def check_target(dump: dict[str, Any], controller: dict[str, Any]) -> None:
    """A dump runs in a container, so the service must link to one (or name it)."""
    if not dump:
        return
    if dump.get("container"):
        if controller.get("type") != "portainer" or "endpoint" not in controller:
            raise ValueError(
                "a dump names a container, which needs the service linked to a Portainer "
                "environment (controller: type portainer, endpoint)"
            )
        return
    if controller.get("type") != "portainer" or not controller.get("container"):
        raise ValueError(
            "a dump runs inside a container: link the service to a Portainer container "
            "(controller: type portainer, endpoint, container) or name one with dump.container"
        )


def target_of(dump: dict[str, Any], controller: dict[str, Any]) -> tuple[int, str]:
    """(Portainer environment, container) the dump runs in."""
    return int(controller["endpoint"]), str(dump.get("container") or controller["container"])


def build_command(cfg: dict[str, Any]) -> list[str]:
    """The argv run in the container. A script, so the container's own environment (and
    its shell) supplies the credentials."""
    kind, db, user = cfg["type"], cfg.get("database", ""), cfg.get("user", "")
    if kind == "postgres":
        who = f'"{user}"' if user else '"${POSTGRES_USER:-postgres}"'
        tool = f'pg_dump -U {who} -d "{db}"' if db else f"pg_dumpall -U {who}"
        script = f'export PGPASSWORD="${{POSTGRES_PASSWORD:-}}"; exec {tool}'
    elif kind in {"mysql", "mariadb"}:
        if user:
            creds = f'export MYSQL_PWD="${{MYSQL_PASSWORD:-}}"; u="{user}"'
        else:
            creds = 'export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD:-}}"; u=root'
        target = f'--databases "{db}"' if db else "--all-databases"
        script = (
            f"{creds}; t=$(command -v mariadb-dump || command -v mysqldump) || "
            f'{{ echo "no mysqldump / mariadb-dump in this container" >&2; exit 127; }}; '
            f'exec "$t" -u "$u" --single-transaction --routines --triggers --events {target}'
        )
    elif kind == "mongo":
        auth = (
            '--username "$MONGO_INITDB_ROOT_USERNAME" --password "$MONGO_INITDB_ROOT_PASSWORD" '
            "--authenticationDatabase admin "
        )
        which = f'--db "{db}" ' if db else ""
        script = (
            f"if [ -n \"${{MONGO_INITDB_ROOT_USERNAME:-}}\" ]; then a='{auth.strip()}'; "
            f'else a=""; fi; exec mongodump $a {which}--archive'
        )
    elif kind == "sqlite":
        script = f'exec sqlite3 "{cfg["path"]}" .dump'
    else:
        script = cfg["command"]
    return ["sh", "-c", script]


def describe(cfg: dict[str, Any]) -> list[str]:
    """Plain-language lines for a normalised ``dump`` block."""
    if not cfg:
        return ["no dump configured"]
    what = cfg["type"] + (f" database {cfg['database']}" if cfg["database"] else "")
    lines = [
        f"dumps       : {'enabled' if cfg['enabled'] else 'switched off'} ({what})",
        f"schedule    : {cfg['schedule'] or 'manual only'}",
        f"keeps       : newest {cfg['keep']}"
        + (f", nothing older than {cfg['keep_days']} days" if cfg["keep_days"] else ""),
    ]
    if cfg["container"]:
        lines.append(f"container   : {cfg['container']}")
    return lines


def restore_hint(cfg: dict[str, Any], container: str, filename: str) -> str:
    """The shell line that puts a dump back, run on the Docker host. Shown, never run: a
    restore overwrites live data, so a person does it on purpose."""
    c, f = container or "<container>", filename or "<file>"
    kind = cfg.get("type")
    if kind == "postgres":
        return (
            f"gunzip -c {f} | docker exec -i {c} "
            'sh -c \'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "${POSTGRES_USER:-postgres}"\''
        )
    if kind in {"mysql", "mariadb"}:
        return (
            f"gunzip -c {f} | docker exec -i {c} sh -c "
            '\'MYSQL_PWD="${MYSQL_ROOT_PASSWORD:-$MARIADB_ROOT_PASSWORD}" '
            "$(command -v mariadb || command -v mysql) -u root'"
        )
    if kind == "mongo":
        return (
            f"gunzip -c {f} | docker exec -i {c} sh -c "
            "'mongorestore --archive --drop "
            '--username "$MONGO_INITDB_ROOT_USERNAME" --password "$MONGO_INITDB_ROOT_PASSWORD" '
            "--authenticationDatabase admin'"
        )
    if kind == "sqlite":
        return f"gunzip -c {f} | docker exec -i {c} sqlite3 {cfg.get('path', '<database file>')}"
    return f"gunzip -c {f}   # restore it the way your command's output is meant to be read"
