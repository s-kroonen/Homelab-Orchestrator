"""The deploy pipeline.

    lock           (switched off? something else running? a guest backup running?)
    pre-guards     probes healthy?  a verified backup newer than N hours, or take one
    snapshot       what is deployed now (Git hash, image ids) - recorded, never secret
    keep previous  (when set) a database dump and a ``pre-deploy-<time>`` tag on each image,
                   keeping the last N
    redeploy       Portainer pulls the Git repository and rebuilds / pulls / recreates
    wait up        every container running and settled
    post-guards    probes healthy in time, then staying healthy
    success | blocked | failed  -> notify; on failure, the application's ``on_failure``

A deploy is a ``PipelineRun`` of kind ``deploy``, so it shows in Activity, the filters,
notifications and push. Where it came from (a person, the CLI, a webhook, Portainer's own
webhook seen from outside) only changes the first step; the guards are the same. A deploy
refused by a *pre*-guard changed nothing and ends ``skipped``; one that went wrong after
Portainer was asked ends ``failed``. See docs/portainer_deploys.md.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.portainer.base import Stack
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import PipelineRun, Service
from orchestrator.db.session import session_scope
from orchestrator.deploy.policy import normalize_deploy
from orchestrator.domain.enums import (
    AuditResult,
    HealthState,
    PipelineKind,
    PipelineStatus,
)
from orchestrator.health.engine import HealthEngine
from orchestrator.logging_setup import get_logger, new_correlation_id
from orchestrator.notify.hooks import announce
from orchestrator.pipelines.restore import (
    RestoreError,
    approval_url,
    create_proposal,
    latest_verified_backup,
)
from orchestrator.pipelines.steps import BackupError, BackupSkipped, StepRecorder
from orchestrator.registry.switch import SwitchRefused, busy_reason, switch_service
from orchestrator.registry.tree import TreeIndex

log = get_logger(__name__)

_background: set[asyncio.Task[None]] = set()


class DeployRefused(Exception):
    """The deploy was not even started (nothing recorded). ``code`` tells the caller why:
    ``no_method`` | ``disabled`` | ``off`` | ``busy`` | ``no_stack`` | ``unknown``."""

    def __init__(self, message: str, code: str = "busy") -> None:
        super().__init__(message)
        self.code = code


KEEP_TAG = "pre-deploy-"


def image_repo(image: str) -> str:
    """``registry:5000/team/app:1.2@sha256:...`` -> ``registry:5000/team/app``."""
    name = image.split("@", 1)[0]
    head, _, last = name.rpartition("/")
    if ":" in last:
        last = last.split(":", 1)[0]
    return f"{head}/{last}" if head else last


class DeployBlocked(Exception):
    """A guard said no before anything was changed."""


class DeployFailed(Exception):
    """Something went wrong after Portainer was asked to redeploy."""


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Trigger:
    """Who or what started the deploy; goes into the first step (never a secret)."""

    source: str  # manual | cli | webhook | outside
    actor: str = "system"
    detail: dict[str, Any] | None = None

    def as_detail(self) -> dict[str, Any]:
        return {"source": self.source, "actor": self.actor, **(self.detail or {})}


class DeployPipeline:
    def __init__(self, adapters: AdapterBundle, settings: Settings) -> None:
        self._adapters = adapters
        self._settings = settings
        self._health = HealthEngine(settings=settings, proxmox=getattr(adapters, "proxmox", None))

    # -- starting -----------------------------------------------------------------------

    def start(
        self,
        session: Session,
        slug: str,
        trigger: Trigger,
        *,
        guards: bool = True,
        guards_only: bool = False,
        reason: str = "",
    ) -> PipelineRun:
        """Check the lock and record a RUNNING run. Raises :class:`DeployRefused`."""
        from orchestrator.web.routers.dashboard import _backup_currently_running

        svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
        if svc is None:
            raise DeployRefused(f"no service {slug!r}", "unknown")
        if not svc.deploy:
            raise DeployRefused(f"{slug!r} has no deploy method", "no_method")
        if not svc.deploy.get("enabled", True):
            raise DeployRefused(f"deploys are switched off for {slug!r}", "disabled")
        tree = TreeIndex.build(session)
        if not tree.is_served(svc):
            raise DeployRefused(f"{slug!r} is switched off - switch it on first", "off")
        busy = busy_reason(session, [s.id for s in [svc, *tree.descendants(svc)]])
        if busy:
            raise DeployRefused(busy, "busy")
        root = tree.root(svc)
        if root.id is not None and _backup_currently_running(session, root.id):
            raise DeployRefused(f"a backup of {root.slug!r} is running", "busy")

        run = PipelineRun(
            kind=PipelineKind.DEPLOY,
            correlation_id=new_correlation_id(),
            service_id=svc.id,
            status=PipelineStatus.RUNNING,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        audit.record(
            session,
            actor=trigger.actor,
            action="deploy.start" if not guards_only else "deploy.guards",
            target=f"service:{slug}",
            correlation_id=run.correlation_id,
            details={
                "run_id": run.id,
                "source": trigger.source,
                "guards": guards,
                **({"unguarded_reason": reason} if not guards else {}),
            },
        )
        if not guards:
            audit.record(
                session,
                actor=trigger.actor,
                action="deploy.unguarded",
                target=f"service:{slug}",
                correlation_id=run.correlation_id,
                details={"run_id": run.id, "reason": reason},
            )
        session.commit()
        return run

    def fire(self, run_id: int, slug: str, trigger: Trigger, **kw: Any) -> None:
        """Continue a started run in the background, detached from the caller."""

        async def go() -> None:
            try:
                await self.execute(run_id, slug, trigger, **kw)
            except Exception:
                log.exception("deploy.background_crashed", run_id=run_id, service=slug)

        task = asyncio.create_task(go(), name=f"deploy:{slug}:{run_id}")
        _background.add(task)
        task.add_done_callback(_background.discard)

    async def execute(
        self,
        run_id: int,
        slug: str,
        trigger: Trigger,
        *,
        guards: bool = True,
        guards_only: bool = False,
        outside: bool = False,
    ) -> None:
        with session_scope() as session:
            run = session.get(PipelineRun, run_id)
            if run is None:
                log.error("deploy.run_missing", run_id=run_id)
                return
            await self._run(
                session, run, slug, trigger, guards=guards, guards_only=guards_only, outside=outside
            )

    async def run_for_service(
        self,
        session: Session,
        slug: str,
        trigger: Trigger,
        *,
        guards: bool = True,
        guards_only: bool = False,
        reason: str = "",
    ) -> PipelineRun:
        """Start and run to the end in one call (CLI, tests)."""
        run = self.start(
            session, slug, trigger, guards=guards, guards_only=guards_only, reason=reason
        )
        await self._run(session, run, slug, trigger, guards=guards, guards_only=guards_only)
        return run

    # -- the run ------------------------------------------------------------------------

    async def _run(
        self,
        session: Session,
        run: PipelineRun,
        slug: str,
        trigger: Trigger,
        *,
        guards: bool = True,
        guards_only: bool = False,
        outside: bool = False,
    ) -> None:
        rec = StepRecorder(session, run)
        svc = session.exec(select(Service).where(Service.slug == slug)).one()
        cfg = normalize_deploy(svc.deploy)
        tree = TreeIndex.build(session)
        rec.record("trigger", "ok", detail={**trigger.as_detail(), "guards": guards})
        try:
            stack = await self._stack(svc)
            if outside:
                rec.record("outside_deploy", "ok", detail=self._describe(stack))
            elif guards:
                await self._pre_guards(session, rec, svc, tree, cfg, trigger, guards_only)
            else:
                rec.record("pre_guards", "skipped", detail={"reason": "override"})
            if guards_only:
                self._finish(session, run, svc, PipelineStatus.SUCCEEDED, None, trigger)
                return
            if not outside:
                snap = await self._snapshot(svc, stack)
                rec.record("snapshot", "ok", detail=snap)
                keep = cfg["guard"]["pre"].get("keep_previous")
                if keep:
                    await self._keep_previous(session, rec, svc, stack, snap, keep, trigger)
                await self._redeploy(rec, svc, stack, cfg)
            await self._wait_up(session, rec, svc, cfg)
            await self._post_guards(session, rec, svc, tree, cfg)
        except DeployBlocked as exc:
            rec.record("blocked", "failed", detail={"error": str(exc)})
            self._finish(session, run, svc, PipelineStatus.SKIPPED, str(exc), trigger)
            await announce(
                session,
                level="sub" if svc.parent_id else "service",
                key=slug,
                name=svc.name or slug,
                event="deploy_blocked",
                severity="warn",
                title=f"Deploy of {slug} was blocked",
                body=f"{exc} Nothing was changed.",
                scope={"slug": slug},
            )
            return
        except DeployFailed as exc:
            rec.record("failed", "failed", detail={"error": str(exc)})
            await announce(
                session,
                level="sub" if svc.parent_id else "service",
                key=slug,
                name=svc.name or slug,
                event="deploy_failed",
                severity="error",
                title=f"Deploy of {slug} failed",
                body=str(exc),
                scope={"slug": slug},
            )
            await self._on_failure(session, rec, run, svc, cfg, str(exc))
            self._finish(session, run, svc, PipelineStatus.FAILED, str(exc), trigger)
            return
        except Exception as exc:  # the run must end up recorded, whatever broke
            log.warning("deploy.crashed", slug=slug, error=str(exc))
            session.rollback()
            rec.record("crashed", "failed", detail={"error": f"{type(exc).__name__}: {exc}"})
            self._finish(session, run, svc, PipelineStatus.FAILED, str(exc), trigger)
            return
        self._finish(session, run, svc, PipelineStatus.SUCCEEDED, None, trigger)
        await announce(
            session,
            level="sub" if svc.parent_id else "service",
            key=slug,
            name=svc.name or slug,
            event="deploy_succeeded",
            severity="info",
            title=f"{slug} was deployed",
            body="Redeployed and healthy." if cfg["guard"]["post"]["probes"] else "Redeployed.",
            scope={"slug": slug},
        )

    def _finish(
        self,
        session: Session,
        run: PipelineRun,
        svc: Service,
        status: PipelineStatus,
        error: str | None,
        trigger: Trigger,
    ) -> None:
        run = session.get(PipelineRun, run.id) or run
        run.status, run.error = status, error
        run.finished_at = datetime.now(UTC)
        session.add(run)
        audit.record(
            session,
            actor=trigger.actor,
            action="deploy.done",
            target=f"service:{svc.slug}",
            result=AuditResult.OK if status is not PipelineStatus.FAILED else AuditResult.ERROR,
            correlation_id=run.correlation_id,
            details={"run_id": run.id, "status": status.value, "error": error},
        )
        session.commit()

    # -- pieces -------------------------------------------------------------------------

    def _portainer(self, svc: Service) -> Any:
        """The Portainer connection this service's stack is on."""
        named = str((svc.controller or {}).get("instance") or "")
        adapter = self._adapters.portainer_for(named)
        if adapter is None:
            raise DeployFailed(f"there is no Portainer connection called {named!r}")
        return adapter

    async def _stack(self, svc: Service) -> Stack:
        cfg = svc.controller or {}
        adapter = self._portainer(svc)
        if not adapter.configured:
            raise DeployFailed("Portainer is not set up")
        try:
            stack = await adapter.get_stack(int(cfg["endpoint"]), str(cfg["stack"]))
        except AdapterError as exc:
            raise DeployFailed(f"Portainer could not be read: {exc}") from exc
        if stack is None:
            raise DeployFailed(f"stack {cfg.get('stack')!r} was not found in Portainer")
        return stack

    @staticmethod
    def _describe(stack: Stack) -> dict[str, Any]:
        git = stack.git
        return {
            "stack": stack.name,
            "git_hash": git.hash if git else "",
            "git_ref": git.reference if git else "",
            "updated_at": stack.updated_at,
        }

    async def _snapshot(self, svc: Service, stack: Stack) -> dict[str, Any]:
        adapter = self._portainer(svc)
        try:
            boxes = await adapter.list_containers(stack.endpoint_id, stack=stack.name)
        except AdapterError:
            boxes = []
        return {
            **self._describe(stack),
            "containers": [
                {"name": c.name, "image": c.image, "image_id": c.image_id, "state": c.state}
                for c in boxes
            ],
        }

    def _subtree(self, tree: TreeIndex, svc: Service) -> list[Service]:
        return [s for s in [svc, *tree.descendants(svc)] if tree.is_served(s)]

    async def _verdicts(
        self, session: Session, services: list[Service]
    ) -> list[tuple[Service, HealthState, str]]:
        out: list[tuple[Service, HealthState, str]] = []
        for s in services:
            verdict = await self._health.scan(session, s)
            out.append((s, verdict.state, verdict.reason))
        return out

    async def _pre_guards(
        self,
        session: Session,
        rec: StepRecorder,
        svc: Service,
        tree: TreeIndex,
        cfg: dict[str, Any],
        trigger: Trigger,
        guards_only: bool,
    ) -> None:
        pre = cfg["guard"]["pre"]
        if pre["probes"]:
            results = await self._verdicts(session, self._subtree(tree, svc))
            bad = [(s, st, why) for s, st, why in results if st is not HealthState.HEALTHY]
            detail = {"services": {s.slug: st.value for s, st, _ in results}}
            if bad:
                s, st, why = bad[0]
                rec.record("guard_probes", "failed", detail={**detail, "error": why})
                raise DeployBlocked(f"{s.slug} is {st.value} before the deploy: {why}.")
            rec.record("guard_probes", "ok", detail=detail)
        else:
            rec.record("guard_probes", "skipped", detail={"reason": "not required"})

        backup = pre["backup"]
        if not backup:
            rec.record("guard_backup", "skipped", detail={"reason": "not required"})
            return
        root = tree.root(svc)
        if root.backup_excluded:
            rec.record("guard_backup", "failed", detail={"error": "excluded from backups"})
            raise DeployBlocked(f"a backup is required, but {root.slug} is excluded from backups.")
        latest = latest_verified_backup(session, root)
        age_h = (
            (datetime.now(UTC) - _aware(latest.created_at)).total_seconds() / 3600
            if latest
            else None
        )
        if latest and age_h is not None and age_h <= backup["max_age_h"]:
            rec.record(
                "guard_backup",
                "ok",
                detail={"backup": latest.pbs_snapshot_id, "age_h": round(age_h, 1)},
            )
            return
        why = (
            f"the newest verified backup is {age_h:.0f}h old (limit {backup['max_age_h']}h)"
            if age_h is not None
            else "there is no verified backup"
        )
        if not backup["take_if_older"]:
            rec.record("guard_backup", "failed", detail={"error": why})
            raise DeployBlocked(f"{why[0].upper()}{why[1:]}.")
        if guards_only:
            rec.record("guard_backup", "ok", detail={"note": f"{why}; a deploy would take one"})
            return
        await self._take_backup(session, rec, root, trigger, why)

    async def _take_backup(
        self, session: Session, rec: StepRecorder, root: Service, trigger: Trigger, why: str
    ) -> None:
        from orchestrator.pipelines.backup import BackupPipeline

        started = datetime.now(UTC)
        try:
            pipeline = BackupPipeline(
                proxmox=self._adapters.proxmox,
                pbs=self._adapters.pbs,
                settings=self._settings,
                power=self._adapters.power,
            )
            backup_run = await pipeline.run_for_service(session, root.slug, actor=trigger.actor)
        except (BackupError, AdapterError) as exc:
            rec.record(
                "guard_backup", "failed", started_at=started, detail={"error": str(exc), "why": why}
            )
            verb = "was skipped" if isinstance(exc, BackupSkipped) else "failed"
            raise DeployBlocked(f"the backup that was needed first {verb}: {exc}") from exc
        rec.record(
            "guard_backup",
            "ok",
            started_at=started,
            detail={"took_backup": True, "backup_run_id": backup_run.id, "why": why},
        )

    async def _keep_previous(
        self,
        session: Session,
        rec: StepRecorder,
        svc: Service,
        stack: Stack,
        snap: dict[str, Any],
        keep: dict[str, Any],
        trigger: Trigger,
    ) -> None:
        """Keep what is deployed now, so a bad version can be rolled back: a database dump (the
        dump's own ``keep`` decides how many stay) and a ``pre-deploy-<time>`` tag on each image
        the stack runs, removing the oldest such tags beyond ``keep``. A dump that fails blocks
        the deploy; an image that cannot be tagged is noted and the deploy goes on."""
        from orchestrator.dumps.pipeline import DumpPipeline, DumpRefused

        started = datetime.now(UTC)
        detail: dict[str, Any] = {"keep": keep["keep"]}
        if keep["dump"] and svc.dump:
            try:
                dump = await DumpPipeline(self._adapters, self._settings).run_for_service(
                    session, svc.slug, trigger.actor
                )
            except DumpRefused as exc:
                rec.record(
                    "keep_previous", "failed", started_at=started, detail={"error": str(exc)}
                )
                raise DeployBlocked(f"the dump before the deploy could not run: {exc}") from exc
            if dump.status is not PipelineStatus.SUCCEEDED:
                rec.record(
                    "keep_previous",
                    "failed",
                    started_at=started,
                    detail={"dump_run_id": dump.id, "error": dump.error},
                )
                raise DeployBlocked(f"the dump before the deploy failed: {dump.error}")
            detail["dump_run_id"] = dump.id
        if keep["images"]:
            detail.update(await self._tag_images(svc, stack, snap, keep["keep"], started))
        rec.record("keep_previous", "ok", started_at=started, detail=detail)

    async def _tag_images(
        self, svc: Service, stack: Stack, snap: dict[str, Any], keep: int, now: datetime
    ) -> dict[str, Any]:
        adapter = self._portainer(svc)
        tag = KEEP_TAG + now.strftime("%Y%m%d-%H%M%S")
        tagged: list[str] = []
        removed: list[str] = []
        problems: list[str] = []
        seen: set[str] = set()
        for box in snap.get("containers") or []:
            repo = image_repo(str(box.get("image") or ""))
            image = str(box.get("image_id") or box.get("image") or "")
            if not repo or not image or repo in seen:
                continue
            seen.add(repo)
            try:
                await adapter.tag_image(stack.endpoint_id, image, repo, tag)
                tagged.append(f"{repo}:{tag}")
                old = sorted(t for t in await adapter.image_tags(stack.endpoint_id, repo)
                             if t.startswith(KEEP_TAG))  # fmt: skip
                for t in old[: max(0, len(old) - keep)]:
                    try:
                        await adapter.remove_image(stack.endpoint_id, f"{repo}:{t}")
                        removed.append(f"{repo}:{t}")
                    except AdapterError as exc:
                        problems.append(f"{repo}:{t} was not removed: {exc}")
            except AdapterError as exc:
                problems.append(f"{repo} was not tagged: {exc}")
        out: dict[str, Any] = {"images": tagged, "removed": removed}
        if problems:
            out["problems"] = problems
        return out

    async def _redeploy(
        self, rec: StepRecorder, svc: Service, stack: Stack, cfg: dict[str, Any]
    ) -> None:
        adapter = self._portainer(svc)
        started = datetime.now(UTC)
        try:
            await adapter.redeploy_stack(stack, pull_image=cfg["pull_image"], prune=cfg["prune"])
        except AdapterError as exc:
            rec.record("redeploy", "failed", started_at=started, detail={"error": str(exc)})
            raise DeployFailed(f"Portainer refused the redeploy: {exc}") from exc
        after = await adapter.get_stack(stack.endpoint_id, stack.name)
        if after is not None:
            from orchestrator.deploy.watch import remember

            remember(svc.slug, after)
        rec.record(
            "redeploy",
            "ok",
            started_at=started,
            detail={"pull_image": cfg["pull_image"], "prune": cfg["prune"]}
            | (self._describe(after) if after else {}),
        )

    async def _ready(self, svc: Service) -> tuple[bool, str]:
        adapter = self._portainer(svc)
        cfg = svc.controller or {}
        try:
            stack = await adapter.get_stack(int(cfg["endpoint"]), str(cfg["stack"]))
            boxes = await adapter.list_containers(int(cfg["endpoint"]), stack=str(cfg["stack"]))
        except AdapterError as exc:
            return False, str(exc)
        if stack is None or stack.status != "active":
            return False, "the stack is not active"
        waiting = [
            c.name for c in boxes if c.state != "running" or c.health in {"starting", "unhealthy"}
        ]
        if not boxes:
            return False, "the stack has no containers"
        return (not waiting, f"waiting for {', '.join(waiting)}" if waiting else "up")

    async def _wait_up(
        self, session: Session, rec: StepRecorder, svc: Service, cfg: dict[str, Any]
    ) -> None:
        started = datetime.now(UTC)
        deadline = time.monotonic() + cfg["guard"]["post"]["timeout_s"]
        while True:
            ok, why = await self._ready(svc)
            if ok:
                rec.record("wait_up", "ok", started_at=started)
                return
            if time.monotonic() >= deadline:
                rec.record("wait_up", "failed", started_at=started, detail={"error": why})
                raise DeployFailed(f"the stack did not come up in time: {why}")
            await asyncio.sleep(self._settings.wake_poll_interval_s)

    async def _post_guards(
        self,
        session: Session,
        rec: StepRecorder,
        svc: Service,
        tree: TreeIndex,
        cfg: dict[str, Any],
    ) -> None:
        post = cfg["guard"]["post"]
        if not post["probes"]:
            rec.record("post_probes", "skipped", detail={"reason": "not required"})
            return
        started = datetime.now(UTC)
        services = self._subtree(tree, svc)
        deadline = time.monotonic() + post["timeout_s"]
        while True:
            results = await self._verdicts(session, services)
            bad = [(s, st, why) for s, st, why in results if st is not HealthState.HEALTHY]
            if not bad:
                break
            if time.monotonic() >= deadline:
                s, st, why = bad[0]
                rec.record("post_probes", "failed", started_at=started, detail={"error": why})
                raise DeployFailed(f"{s.slug} is {st.value} after the deploy: {why}")
            await asyncio.sleep(self._settings.wake_poll_interval_s)
        rec.record("post_probes", "ok", started_at=started)

        settle_from = time.monotonic()
        started = datetime.now(UTC)
        while time.monotonic() - settle_from < post["settle_s"]:
            await asyncio.sleep(self._settings.wake_poll_interval_s)
            results = await self._verdicts(session, services)
            bad = [(s, st, why) for s, st, why in results if st is not HealthState.HEALTHY]
            if bad:
                s, st, why = bad[0]
                rec.record("settle", "failed", started_at=started, detail={"error": why})
                raise DeployFailed(f"{s.slug} became {st.value} while settling: {why}")
        rec.record("settle", "ok", started_at=started, detail={"seconds": post["settle_s"]})

    async def _on_failure(
        self,
        session: Session,
        rec: StepRecorder,
        run: PipelineRun,
        svc: Service,
        cfg: dict[str, Any],
        error: str,
    ) -> None:
        action = cfg["on_failure"]
        if action == "notify":
            rec.record("on_failure", "ok", detail={"action": "notify", "note": "left as it is"})
            return
        if action == "stop":
            try:
                result = await switch_service(
                    session,
                    self._adapters,
                    self._settings,
                    svc.slug,
                    False,
                    actor="deploy",
                    ignore_run=run.id,
                )
            except (SwitchRefused, LookupError) as exc:
                rec.record("on_failure", "failed", detail={"action": "stop", "error": str(exc)})
                return
            rec.record(
                "on_failure",
                "ok" if result.ok else "failed",
                detail={
                    "action": "stop",
                    "switch_run": result.run_id,
                    "error": result.message if not result.ok else "",
                },
            )
            return
        # restore_backup: only ever proposed - it throws data away
        tree = TreeIndex.build(session)
        root = tree.root(svc)
        try:
            approval = create_proposal(
                session,
                self._settings,
                root,
                requested_by="deploy",
                reason=f"the deploy of {svc.slug} failed: {error}"[:400],
            )
            session.flush()
        except RestoreError as exc:
            rec.record(
                "on_failure", "failed", detail={"action": "restore_backup", "error": str(exc)}
            )
            return
        session.commit()
        rec.record(
            "on_failure",
            "ok",
            detail={"action": "restore_backup", "snapshot": approval.payload.get("snapshot")},
        )
        url = approval_url(self._settings, approval.token)
        await announce(
            session,
            level="service",
            key=root.slug,
            name=root.name or root.slug,
            event="restore_proposed",
            severity="error",
            title=f"Restore proposed for {root.name or root.slug}",
            body=(
                f"The deploy of {svc.slug} failed. Restoring {approval.payload.get('snapshot')} "
                "would replace the whole guest with the copy from before. Nothing happens "
                "until you approve." + ("" if url else f" Open /approvals/{approval.token}.")
            ),
            scope={"slug": root.slug},
            approve_url=url,
        )
