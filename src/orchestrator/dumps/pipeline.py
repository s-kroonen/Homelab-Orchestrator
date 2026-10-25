"""The dump pipeline: one database dumped on its own.

    check     configured? switched on? not already running? the node and guest up, the container running
    dump      the database's own tool runs inside the container (Portainer exec); its output is
              compressed into ``<DUMP_DIR>/<slug>/<time>.<ext>.part`` as it arrives
    verify    the file reads back whole (gzip CRC), is not tiny, and ends the way that tool's
              output ends (so a dump cut off half way is a failure, not a backup)
    record    the file is renamed into place and a ``DumpRecord`` written
    prune     only the newest ``keep`` (and nothing older than ``keep_days``) stay; the newest
              verified one and protected ones are never removed

A dump is a ``PipelineRun`` of kind ``dump``, so it shows in Activity, notifications and push.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import DumpRecord, PipelineRun, Service
from orchestrator.db.session import session_scope
from orchestrator.domain.enums import AuditResult, PipelineKind, PipelineStatus
from orchestrator.dumps.policy import EXTENSION, build_command, normalize_dump, target_of
from orchestrator.logging_setup import get_logger, new_correlation_id
from orchestrator.notify.hooks import announce
from orchestrator.pipelines.steps import StepRecorder
from orchestrator.registry.linked import gather_context, host_check

log = get_logger(__name__)

_background: set[asyncio.Task[None]] = set()

#: What each tool writes last; a dump without it was cut off.
END_MARKERS: dict[str, bytes] = {
    "postgres": b"dump complete",
    "mysql": b"Dump completed",
    "mariadb": b"Dump completed",
    "sqlite": b"COMMIT;",
}
_TAIL = 4096


class DumpRefused(Exception):
    """Not even started (nothing recorded). ``code``: unknown | no_dump | disabled | busy."""

    def __init__(self, message: str, code: str = "busy") -> None:
        super().__init__(message)
        self.code = code


class DumpFailed(Exception):
    """The dump ran (or tried to) and did not produce a usable file."""


def dump_root(settings: Settings) -> Path:
    return (
        Path(settings.dump_dir.strip())
        if settings.dump_dir.strip()
        else settings.state_dir / "dumps"
    )


def file_of(settings: Settings, record: DumpRecord) -> Path:
    """The record's file, refusing anything that points outside the dump directory."""
    root = dump_root(settings).resolve()
    path = (root / record.path).resolve()
    if root not in path.parents:
        raise ValueError("dump path escapes the dump directory")
    return path


@dataclass
class Checked:
    size: int
    sha256: str
    uncompressed: int


def verify_file(path: Path, kind: str, min_bytes: int) -> Checked:
    """Read the whole file back. Raises :class:`DumpFailed` for anything but a good dump."""
    digest = hashlib.sha256()
    with path.open("rb") as raw:
        for block in iter(lambda: raw.read(1 << 20), b""):
            digest.update(block)
    tail = b""
    total = 0
    try:
        with gzip.open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                total += len(block)
                tail = (tail + block)[-_TAIL:]
    except (OSError, EOFError) as exc:
        raise DumpFailed(f"the dump file does not read back whole: {exc}") from exc
    if total < min_bytes:
        raise DumpFailed(f"the dump is only {total} bytes (at least {min_bytes} expected)")
    marker = END_MARKERS.get(kind)
    if marker is not None and marker not in tail:
        raise DumpFailed(f"the dump does not end the way a complete {kind} dump does (cut off?)")
    return Checked(size=path.stat().st_size, sha256=digest.hexdigest(), uncompressed=total)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def prune(session: Session, settings: Settings, svc: Service, cfg: dict[str, Any]) -> list[str]:
    """Remove dumps beyond ``keep`` / ``keep_days``. The newest verified dump and protected
    ones stay whatever the limits say. Returns the paths removed."""
    rows = session.exec(
        select(DumpRecord)
        .where(DumpRecord.service_slug == svc.slug)
        .order_by(col(DumpRecord.created_at).desc(), col(DumpRecord.id).desc())
    ).all()
    newest_good = next((r.id for r in rows if r.verified), None)
    cutoff = datetime.now(UTC) - timedelta(days=cfg["keep_days"]) if cfg["keep_days"] else None
    removed: list[str] = []
    kept = 0
    for r in rows:
        if r.protected or r.id == newest_good:
            kept += 1
            continue
        too_many = kept >= cfg["keep"]
        too_old = cutoff is not None and _aware(r.created_at) < cutoff
        if too_many or too_old:
            remove_record(session, settings, r)
            removed.append(r.path)
        else:
            kept += 1
    return removed


def remove_record(session: Session, settings: Settings, record: DumpRecord) -> None:
    try:
        file_of(settings, record).unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        log.warning("dump.remove_failed", path=record.path, error=str(exc))
    session.delete(record)


class DumpPipeline:
    def __init__(self, adapters: AdapterBundle, settings: Settings) -> None:
        self._adapters = adapters
        self._settings = settings

    # -- starting -----------------------------------------------------------------------

    def start(self, session: Session, slug: str, actor: str = "system") -> PipelineRun:
        """Check the lock and record a RUNNING run. Raises :class:`DumpRefused`."""
        svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
        if svc is None:
            raise DumpRefused(f"no service {slug!r}", "unknown")
        if not svc.dump:
            raise DumpRefused(f"{slug!r} has no dump configured", "no_dump")
        if not svc.dump.get("enabled", True):
            raise DumpRefused(f"dumps are switched off for {slug!r}", "disabled")
        running = session.exec(
            select(PipelineRun).where(
                PipelineRun.kind == PipelineKind.DUMP,
                PipelineRun.service_id == svc.id,
                PipelineRun.status == PipelineStatus.RUNNING,
            )
        ).first()
        if running is not None:
            raise DumpRefused(f"a dump of {slug!r} is already running (run {running.id})", "busy")
        run = PipelineRun(
            kind=PipelineKind.DUMP,
            correlation_id=new_correlation_id(),
            service_id=svc.id,
            status=PipelineStatus.RUNNING,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        audit.record(
            session,
            actor=actor,
            action="dump.start",
            target=f"service:{slug}",
            correlation_id=run.correlation_id,
            details={"run_id": run.id},
        )
        session.commit()
        return run

    def fire(self, run_id: int, slug: str, actor: str = "system") -> None:
        async def go() -> None:
            try:
                await self.execute(run_id, slug, actor)
            except Exception:
                log.exception("dump.background_crashed", run_id=run_id, service=slug)

        task = asyncio.create_task(go(), name=f"dump:{slug}:{run_id}")
        _background.add(task)
        task.add_done_callback(_background.discard)

    async def execute(self, run_id: int, slug: str, actor: str = "system") -> None:
        with session_scope() as session:
            run = session.get(PipelineRun, run_id)
            if run is None:
                log.error("dump.run_missing", run_id=run_id)
                return
            await self._run(session, run, slug, actor)

    async def run_for_service(
        self, session: Session, slug: str, actor: str = "system"
    ) -> PipelineRun:
        """Start and run to the end in one call (the scheduler, the CLI, tests)."""
        run = self.start(session, slug, actor)
        await self._run(session, run, slug, actor)
        return run

    # -- the run ------------------------------------------------------------------------

    async def _run(self, session: Session, run: PipelineRun, slug: str, actor: str) -> None:
        rec = StepRecorder(session, run)
        svc = session.exec(select(Service).where(Service.slug == slug)).one()
        cfg = normalize_dump(svc.dump)
        part: Path | None = None
        try:
            endpoint, container = await self._check(session, rec, svc, cfg)
            part, final = self._paths(slug, cfg)
            outcome = await self._dump(rec, endpoint, container, cfg, part)
            checked = await self._verify(rec, part, cfg, outcome)
            record = self._record(session, rec, run, svc, cfg, part, final, checked)
            removed = prune(session, self._settings, svc, cfg)
            session.commit()
            rec.record("prune", "ok", detail={"removed": removed})
        except DumpFailed as exc:
            session.rollback()
            if part is not None:
                part.unlink(missing_ok=True)
            rec.record("failed", "failed", detail={"error": str(exc)})
            self._finish(session, run, svc, PipelineStatus.FAILED, str(exc), actor)
            await announce(
                session,
                level="sub" if svc.parent_id else "service",
                key=slug,
                name=svc.name or slug,
                event="dump_failed",
                severity="error",
                title=f"Dump of {slug} failed",
                body=str(exc),
                scope={"slug": slug},
            )
            return
        except Exception as exc:  # the run must end up recorded, whatever broke
            log.warning("dump.crashed", slug=slug, error=str(exc))
            session.rollback()
            if part is not None:
                part.unlink(missing_ok=True)
            rec.record("crashed", "failed", detail={"error": f"{type(exc).__name__}: {exc}"})
            self._finish(session, run, svc, PipelineStatus.FAILED, str(exc), actor)
            return
        self._finish(session, run, svc, PipelineStatus.SUCCEEDED, None, actor)
        await announce(
            session,
            level="sub" if svc.parent_id else "service",
            key=slug,
            name=svc.name or slug,
            event="dump_succeeded",
            severity="info",
            title=f"{slug} was dumped",
            body=f"{_human(record.size_bytes)} kept as {record.path}.",
            scope={"slug": slug},
        )

    def _finish(
        self,
        session: Session,
        run: PipelineRun,
        svc: Service,
        status: PipelineStatus,
        error: str | None,
        actor: str,
    ) -> None:
        run = session.get(PipelineRun, run.id) or run
        run.status, run.error = status, error
        run.finished_at = datetime.now(UTC)
        session.add(run)
        audit.record(
            session,
            actor=actor,
            action="dump.done",
            target=f"service:{svc.slug}",
            result=AuditResult.OK if status is PipelineStatus.SUCCEEDED else AuditResult.ERROR,
            correlation_id=run.correlation_id,
            details={"run_id": run.id, "status": status.value, "error": error},
        )
        session.commit()

    # -- pieces -------------------------------------------------------------------------

    async def _check(
        self, session: Session, rec: StepRecorder, svc: Service, cfg: dict[str, Any]
    ) -> tuple[int, str]:
        portainer = getattr(self._adapters, "portainer", None)
        if portainer is None or not portainer.configured:
            raise DumpFailed("Portainer is not set up")
        try:
            endpoint, container = target_of(cfg, svc.controller or {})
        except (KeyError, ValueError):
            raise DumpFailed("the service is not linked to a Portainer container") from None
        ctx = await gather_context(session, self._adapters, light=True)
        host, _ = await host_check(session, ctx, svc)
        if host is not None and host.down:
            raise DumpFailed(f"nothing to dump from: {host.message}")
        try:
            detail = await portainer.get_container(endpoint, container)
        except AdapterError as exc:
            raise DumpFailed(f"Portainer could not be read: {exc}") from exc
        if detail is None:
            raise DumpFailed(f"container {container!r} was not found in Portainer")
        if detail.container.state != "running":
            raise DumpFailed(f"container {container!r} is {detail.container.state}, not running")
        rec.record("check", "ok", detail={"endpoint": endpoint, "container": container})
        return endpoint, container

    def _paths(self, slug: str, cfg: dict[str, Any]) -> tuple[Path, Path]:
        folder = dump_root(self._settings) / slug
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        final = folder / f"{stamp}.{EXTENSION[cfg['type']]}"
        n = 1
        while final.exists():  # two dumps inside one second
            final = folder / f"{stamp}-{n}.{EXTENSION[cfg['type']]}"
            n += 1
        return final.with_name(final.name + ".part"), final

    async def _dump(
        self, rec: StepRecorder, endpoint: int, container: str, cfg: dict[str, Any], part: Path
    ) -> Any:
        portainer = self._adapters.portainer
        assert portainer is not None  # _check already said so
        started = datetime.now(UTC)
        with part.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as gz:
            try:
                outcome = await asyncio.wait_for(
                    portainer.exec_stream(
                        endpoint,
                        container,
                        build_command(cfg),
                        lambda chunk: None if gz.write(chunk) is None else None,
                        timeout_s=float(cfg["timeout_s"]),
                    ),
                    timeout=float(cfg["timeout_s"]),
                )
            except TimeoutError:
                raise DumpFailed(f"the dump did not finish in {cfg['timeout_s']} s") from None
            except AdapterError as exc:
                raise DumpFailed(str(exc)) from exc
        if outcome.exit_code not in (0, None):
            tail = outcome.stderr[-300:] or "no message"
            raise DumpFailed(f"the dump command failed (exit {outcome.exit_code}): {tail}")
        rec.record(
            "dump",
            "ok",
            detail={
                "bytes_out": outcome.stdout_bytes,
                "exit_code": outcome.exit_code,
                **({"note": "exit code not reported"} if outcome.exit_code is None else {}),
                **({"stderr": outcome.stderr[-300:]} if outcome.stderr else {}),
            },
            started_at=started,
        )
        return outcome

    async def _verify(
        self, rec: StepRecorder, part: Path, cfg: dict[str, Any], outcome: Any
    ) -> Checked:
        started = datetime.now(UTC)
        checked = await asyncio.to_thread(verify_file, part, cfg["type"], cfg["min_bytes"])
        rec.record(
            "verify",
            "ok",
            detail={"size": checked.size, "uncompressed": checked.uncompressed},
            started_at=started,
        )
        return checked

    def _record(
        self,
        session: Session,
        rec: StepRecorder,
        run: PipelineRun,
        svc: Service,
        cfg: dict[str, Any],
        part: Path,
        final: Path,
        checked: Checked,
    ) -> DumpRecord:
        shutil.move(str(part), str(final))
        record = DumpRecord(
            service_id=svc.id,
            service_slug=svc.slug,
            pipeline_run_id=run.id,
            path=f"{svc.slug}/{final.name}",
            kind=cfg["type"],
            size_bytes=checked.size,
            sha256=checked.sha256,
            verified=True,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        rec.record("record", "ok", detail={"path": record.path, "sha256": record.sha256[:12]})
        return record


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"
