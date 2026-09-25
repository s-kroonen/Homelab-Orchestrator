"""Database integrity probes — the root-cause defence.

Repeated power failures caused unclean shutdowns that corrupted in-flight DB
state. These probes exist so a corrupt database is caught *before* its backup
overwrites a known-good one.

Every one of them works the same way, and it matters:

    produce a FRESH dump, then validate THAT

Never the live datafile. A live file is being written to, so a clean read of it
proves nothing about consistency, and a dirty read looks like corruption when it
is only concurrency. A dump that the engine completes and a validator accepts is
evidence the engine can still walk its own structures end to end.

These are slow by nature. Set ``timeout_s`` generously — a MariaDB dump of a real
database is minutes, not seconds.
"""

from __future__ import annotations

from collections.abc import Sequence

from orchestrator.db.models import Probe as ProbeRow
from orchestrator.domain.enums import HealthState, ProbeKind
from orchestrator.domain.schemas import ProbeResult
from orchestrator.health.probes.base import register_probe
from orchestrator.health.probes.commands import _CommandProbe
from orchestrator.health.transports.base import CommandResult
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


@register_probe
class RedisIntegrityProbe(_CommandProbe):
    """Trigger BGSAVE, wait for it to land, then redis-check-rdb the result.

    Runs through ``sh -lc`` because it is genuinely a sequence with a wait in the
    middle; the command string is built here from validated config, not taken
    from YAML verbatim.
    """

    kind = ProbeKind.DB_REDIS

    def _argv(self, row: ProbeRow) -> Sequence[str]:
        dump_path = str(row.config.get("dump_path", "/var/lib/redis/dump.rdb"))
        trigger = bool(row.config.get("trigger_bgsave", True))
        cli = str(row.config.get("redis_cli", "redis-cli"))
        checker = str(row.config.get("redis_check_rdb", "redis-check-rdb"))

        if not trigger:
            return [checker, dump_path]

        # rdb_bgsave_in_progress flips to 0 when the fork finishes. Poll it rather
        # than sleeping a guess, and fail loudly if it never completes.
        script = (
            f"set -e; "
            f"{cli} BGSAVE; "
            f"for i in $(seq 1 120); do "
            f'  if {cli} INFO persistence | grep -q "rdb_bgsave_in_progress:0"; then break; fi; '
            f"  sleep 1; "
            f"done; "
            f'{cli} INFO persistence | grep -q "rdb_last_bgsave_status:ok" '
            f'|| {{ echo "BGSAVE did not report ok" >&2; exit 1; }}; '
            f"{checker} {dump_path}"
        )
        return ["sh", "-lc", script]


@register_probe
class MongoIntegrityProbe(_CommandProbe):
    """mongodump to /dev/null — exercises a full read of every collection.

    A dump that completes means WiredTiger walked all its structures. We discard
    the output because we only want the read path exercised; PBS holds the real
    backup.
    """

    kind = ProbeKind.DB_MONGO

    def _argv(self, row: ProbeRow) -> Sequence[str]:
        uri = str(row.config["mongodump_uri"])
        db = row.config.get("validate_db")
        argv = ["mongodump", "--uri", uri, "--archive=/dev/null", "--quiet"]
        if db:
            argv += ["--db", str(db)]
        return argv


@register_probe
class SqliteIntegrityProbe(_CommandProbe):
    """PRAGMA integrity_check against a snapshot copy.

    Uses ``.backup`` (SQLite's online backup API) rather than cp, so the copy is
    transactionally consistent even while the app is writing.
    """

    kind = ProbeKind.DB_SQLITE

    def _argv(self, row: ProbeRow) -> Sequence[str]:
        db_path = str(row.config["db_path"])
        tmp = str(row.config.get("temp_path", "/tmp/orchestrator-sqlite-check.db"))
        script = (
            f"set -e; "
            f"rm -f {tmp}; "
            f'sqlite3 "{db_path}" ".backup \'{tmp}\'"; '
            f'result=$(sqlite3 "{tmp}" "PRAGMA integrity_check;"); '
            f"rm -f {tmp}; "
            f'echo "$result"; '
            f'[ "$result" = "ok" ]'
        )
        return ["sh", "-lc", script]

    def _interpret(self, row: ProbeRow, result: CommandResult) -> ProbeResult:
        output = result.stdout.strip()
        if result.ok:
            return self._result(
                row,
                HealthState.HEALTHY,
                message="PRAGMA integrity_check returned ok",
                latency_ms=result.duration_ms,
                details={"db_path": row.config.get("db_path")},
            )
        return self._result(
            row,
            HealthState.FAILED,
            message=f"integrity_check did not return ok: {output or result.tail()}",
            latency_ms=result.duration_ms,
            details={
                "db_path": row.config.get("db_path"),
                "integrity_check_output": output[:2000],
            },
        )


@register_probe
class MariadbIntegrityProbe(_CommandProbe):
    """mariadb-check, then a single-transaction dump to /dev/null.

    Both halves matter: ``mariadb-check`` inspects table structures, while the
    dump proves every row can actually be read. ``--single-transaction`` keeps it
    from locking a live database.

    Credentials come from a defaults-file on the guest, never from this config —
    a password in services.yaml would end up in the YAML the dashboard saves.
    """

    kind = ProbeKind.DB_MARIADB

    def _argv(self, row: ProbeRow) -> Sequence[str]:
        defaults_file = str(row.config["mysql_defaults_file"])
        databases = [str(d) for d in row.config.get("databases", [])]
        check_bin = str(row.config.get("check_bin", "mariadb-check"))
        dump_bin = str(row.config.get("dump_bin", "mariadb-dump"))

        if databases:
            db_args = " ".join(f'"{d}"' for d in databases)
            check = f"{check_bin} --defaults-extra-file={defaults_file} --check {db_args}"
            dump = (
                f"{dump_bin} --defaults-extra-file={defaults_file} "
                f"--single-transaction --databases {db_args} > /dev/null"
            )
        else:
            check = f"{check_bin} --defaults-extra-file={defaults_file} --check --all-databases"
            dump = (
                f"{dump_bin} --defaults-extra-file={defaults_file} "
                f"--single-transaction --all-databases > /dev/null"
            )

        return ["sh", "-lc", f"set -e; {check}; {dump}"]
