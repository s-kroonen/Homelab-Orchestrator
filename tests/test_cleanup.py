"""OR-96: cleanup policies prune and garbage-collect a PBS datastore while it is awake, in a
queue that wakes the storage once for a cleanup and the backups around it."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.adapters.pbs.base import DatastoreStatus, PbsTask, Snapshot
from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import (
    AuditEntry,
    BackupPolicy,
    BackupRecord,
    CleanupPolicy,
    Node,
    PipelineRun,
    Service,
)
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import (
    CleanupGcMode,
    CleanupWhen,
    GuestKind,
    PipelineKind,
    PipelineStatus,
)
from orchestrator.pipelines.backup import BackupPipeline
from orchestrator.pipelines.backup_wake import WakeContext
from orchestrator.pipelines.cleanup import CleanupPipeline, gc_progress, gc_summary
from orchestrator.registry.loader import read_yaml_file, validate_references, write_yaml_file
from orchestrator.registry.schema import RegistryFile
from tests import test_backup_wake as base
from tests.test_backup_retry_restore import OffPower, StopProxmox
from tests.test_backup_retry_restore import announced as announced  # the fixture
from tests.test_backup_wake import StubHealth, World, WorldPbs, _scheduler, _world

topo = base.topo
_fast_wake = base._fast_wake

GC_LOG_RUNNING = [
    "starting garbage collection on store main",
    "Start GC phase1 (mark used chunks)",
    "marked 31% (620 of 2000 index files)",
    "marked 62% (1240 of 2000 index files)",
]
GC_LOG_DONE = [
    *GC_LOG_RUNNING,
    "Start GC phase2 (sweep unused chunks)",
    "processed 50% (12345 chunks)",
    "Removed garbage: 41.2 GiB",
    "Removed chunks: 21099",
    "Pending removals: 12 GiB (in 6000 chunks)",
    "TASK OK",
]


class CleanPbs(WorldPbs):
    """A PBS with a datastore, snapshots and a GC that runs for a few polls."""

    def __init__(self, world: World, *, polls: int = 2, exit_status: str = "OK") -> None:
        super().__init__(world)
        self.polls_left = polls
        self.exit_status = exit_status
        self.snaps: list[Snapshot] = []
        self.free = 16 * 1024**3
        self.stopped: list[str] = []
        self.pruned: list[tuple[str, dict[str, Any]]] = []
        self.gate: asyncio.Event | None = None

    async def datastore_status(self, name: str) -> DatastoreStatus:
        self.w.events.append("pbs_status")
        if self.gate is not None:
            await self.gate.wait()  # hold the job here (a queue test looks at it mid-run)
        total = 400 * 1024**3
        return DatastoreStatus(
            name=name,
            total_bytes=total,
            used_bytes=total - self.free,
            available_bytes=self.free,
            reachable=True,
        )

    async def start_garbage_collection(self, datastore: str) -> str:
        self.w.events.append("gc")
        return "UPID:pbs:0001:0001:0001:garbage_collection:main:token:"

    async def task(self, upid: str, *, log_lines: int = 50) -> PbsTask:
        if self.polls_left > 0:
            self.polls_left -= 1
            return PbsTask(upid=upid, running=True, log_tail=GC_LOG_RUNNING)
        self.free = 60 * 1024**3
        return PbsTask(upid=upid, running=False, exit_status=self.exit_status, log_tail=GC_LOG_DONE)

    async def stop_task(self, upid: str) -> None:
        self.stopped.append(upid)

    async def list_snapshots(self, datastore: str, **kw: Any) -> list[Snapshot]:
        if kw.get("backup_id"):  # the backup locating what it just dumped
            return await super().list_snapshots(datastore, **kw)
        return list(self.snaps)

    async def prune(self, **kw: Any) -> list[Snapshot]:
        self.w.events.append(f"prune:{kw['backup_type']}/{kw['backup_id']}")
        self.pruned.append((f"{kw['backup_type']}/{kw['backup_id']}", kw))
        return []


def _cleanup(session: Session, **kw: Any) -> CleanupPolicy:
    c = CleanupPolicy(name=kw.pop("name", "nightly-cleanup"), requires=["truenas"], **kw)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _snap(group: str, when: datetime) -> Snapshot:
    t, i = group.split("/")
    return Snapshot(datastore="example-datastore", backup_type=t, backup_id=i,
                    backup_time=int(when.timestamp()))  # fmt: skip


def _pipe(world: World, pbs: Any) -> BackupPipeline:
    return BackupPipeline(
        proxmox=StopProxmox(world),
        pbs=pbs,
        settings=get_settings(),
        health=StubHealth(world),  # type: ignore[arg-type]
        power=OffPower(world),
    )


def _steps(run: PipelineRun) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in run.steps}


@pytest.fixture(autouse=True)
def _quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "backup_power_off_grace_s", 0)
    monkeypatch.setattr(get_settings(), "cleanup_poll_s", 0.001)


# -- the pipeline ------------------------------------------------------------------------


async def test_a_cleanup_prunes_by_policy_then_waits_for_the_gc(
    session: Session, topo: dict[str, Any]
) -> None:
    topo["policy"].retention = {"keep_daily": 7}
    session.add(topo["policy"])
    session.commit()
    world = _world(topo)
    pbs = CleanPbs(world)
    now = datetime.now(UTC)
    pbs.snaps = [_snap("vm/101", now), _snap("vm/120", now)]  # vm/120: nobody's
    c = _cleanup(session)

    run = await CleanupPipeline(pbs=pbs, settings=get_settings()).run(
        session, c, prepared=WakeContext(), actor="t"
    )

    assert run.status is PipelineStatus.SUCCEEDED, run.error
    s = _steps(run)
    assert s["cleanup"]["detail"]["policy"] == "nightly-cleanup"
    assert s["space_before"]["detail"]["available_bytes"] == 16 * 1024**3
    assert s["prune"]["status"] == "ok"
    assert s["prune"]["detail"]["unowned"] == ["vm/120"]
    assert s["prune"]["detail"]["unowned_action"] == "left alone"
    assert {g for g, _ in pbs.pruned} == {"vm/101", "vm/102", "vm/103"}  # each service, by policy
    # the GC step was updated in place while it ran, then finished with PBS's numbers
    assert [x["name"] for x in run.steps].count("gc") == 1
    assert s["gc"]["status"] == "ok"
    assert s["gc"]["detail"]["removed"] == "41.2 GiB"
    assert s["gc"]["detail"]["pending"] == "12 GiB"
    assert s["space_after"]["detail"]["available_bytes"] == 60 * 1024**3
    assert s["sync_records"]["status"] == "ok"


async def test_groups_no_policy_keeps_are_pruned_only_when_asked(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo)
    pbs = CleanPbs(world, polls=0)
    pbs.snaps = [_snap("vm/120", datetime.now(UTC))]
    c = _cleanup(session, prune_unowned_keep_last=3, gc_mode=CleanupGcMode.OFF)

    run = await CleanupPipeline(pbs=pbs, settings=get_settings()).run(
        session, c, prepared=WakeContext(), actor="t"
    )

    assert any(g == "vm/120" and kw["keep_last"] == 3 for g, kw in pbs.pruned)
    assert _steps(run)["prune"]["detail"]["unowned_action"] == "kept the last 3"
    assert _steps(run)["gc"]["status"] == "skipped"


async def test_gc_is_not_run_again_within_its_interval(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo)
    pbs = CleanPbs(world, polls=0)
    c = _cleanup(session, prune=False, gc_interval_h=20)
    pipe = CleanupPipeline(pbs=pbs, settings=get_settings())

    first = await pipe.run(session, c, prepared=WakeContext(), actor="t")
    second = await pipe.run(session, c, prepared=WakeContext(), actor="t")

    assert _steps(first)["gc"]["status"] == "ok"
    assert _steps(second)["gc"]["status"] == "skipped"
    assert "at most every 20 h" in _steps(second)["gc"]["detail"]["reason"]
    assert world.count("gc") == 1


async def test_gc_above_runs_only_when_fuller_than_the_threshold(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo)
    pbs = CleanPbs(world, polls=0)
    pbs.free = 200 * 1024**3  # 50% full
    c = _cleanup(session, prune=False, gc_mode=CleanupGcMode.ABOVE, gc_above_pct=80)

    run = await CleanupPipeline(pbs=pbs, settings=get_settings()).run(
        session, c, prepared=WakeContext(), actor="t"
    )

    assert _steps(run)["gc"]["status"] == "skipped"
    assert "under the policy's 80%" in _steps(run)["gc"]["detail"]["reason"]


async def test_a_failed_gc_fails_the_run_with_pbs_s_reason_and_is_announced(
    session: Session, topo: dict[str, Any], announced: Any
) -> None:
    world = _world(topo)
    pbs = CleanPbs(world, polls=0, exit_status="atime safety check failed")

    class QuotaPbs(CleanPbs):
        async def task(self, upid: str, *, log_lines: int = 50) -> PbsTask:
            return PbsTask(
                upid=upid,
                running=False,
                exit_status="atime safety check failed",
                log_tail=[
                    "starting garbage collection on store main",
                    "TASK ERROR: atime safety check failed: update atime failed for chunk/file "
                    '"/mnt/pbs/.chunks/bb9f/bb9f…" - EDQUOT: Quota exceeded',
                ],
            )

    pbs = QuotaPbs(world)
    c = _cleanup(session, prune=False)

    run = await CleanupPipeline(pbs=pbs, settings=get_settings()).run(
        session, c, prepared=WakeContext(), actor="t"
    )

    assert run.status is PipelineStatus.FAILED
    assert "EDQUOT: Quota exceeded" in (run.error or "")
    assert _steps(run)["gc"]["status"] == "failed"
    assert "cleanup_failed" in announced.kinds()


async def test_a_gc_past_the_policy_s_wait_is_stopped_in_pbs(
    session: Session, topo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(topo)
    pbs = CleanPbs(world, polls=10**6)  # never finishes
    c = _cleanup(session, prune=False, max_wait_h=1)
    clock = iter(range(0, 10**9, 1800))  # half an hour per look
    monkeypatch.setattr("orchestrator.pipelines.cleanup.time.monotonic", lambda: next(clock))

    run = await CleanupPipeline(pbs=pbs, settings=get_settings()).run(
        session, c, prepared=WakeContext(), actor="t"
    )

    assert run.status is PipelineStatus.FAILED
    assert pbs.stopped == ["UPID:pbs:0001:0001:0001:garbage_collection:main:token:"]
    assert "still running after 1 h" in _steps(run)["gc"]["detail"]["error"]


async def test_stopping_a_running_cleanup_stops_its_gc(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo)
    pbs = CleanPbs(world, polls=10**6)
    c = _cleanup(session, prune=False)
    stop = asyncio.Event()
    stop.set()

    run = await CleanupPipeline(pbs=pbs, settings=get_settings()).run(
        session, c, prepared=WakeContext(), actor="t", stop=stop
    )

    assert run.status is PipelineStatus.FAILED
    assert pbs.stopped
    assert "stopped by request" in (run.error or "")


async def test_storage_that_could_not_be_woken_fails_the_cleanup(
    session: Session, topo: dict[str, Any]
) -> None:
    world = _world(topo)
    pbs = CleanPbs(world)
    c = _cleanup(session)

    run = await CleanupPipeline(pbs=pbs, settings=get_settings()).run(
        session, c, prepared=WakeContext(error="storage-vm could not be woken"), actor="t"
    )

    assert run.status is PipelineStatus.FAILED
    assert _steps(run)["storage"]["status"] == "failed"
    assert "gc" not in world.events


async def test_backup_records_whose_snapshot_is_gone_are_dropped(
    session: Session, topo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "dry_run", False)
    world = _world(topo)
    pbs = CleanPbs(world, polls=0)
    kept = _snap("vm/101", datetime(2026, 11, 20, tzinfo=UTC))
    pbs.snaps = [kept]
    for sid in (kept.snapshot_id, "vm/101/2026-10-01T02:00:00Z"):
        session.add(BackupRecord(service_slug="app", pbs_snapshot_id=sid))
    session.commit()
    c = _cleanup(session, prune=False, gc_mode=CleanupGcMode.OFF)

    run = await CleanupPipeline(pbs=pbs, settings=get_settings()).run(
        session, c, prepared=WakeContext(), actor="t"
    )

    assert _steps(run)["sync_records"]["detail"] == {"dropped": 1, "kept": 1, "verified": 0}
    left = [r.pbs_snapshot_id for r in session.exec(select(BackupRecord)).all()]
    assert left == [kept.snapshot_id]


def test_gc_log_parsing() -> None:
    assert gc_progress(GC_LOG_RUNNING) == {"phase": 1, "percent": 62, "indexes": 1240, "of": 2000}
    assert gc_progress(GC_LOG_DONE)["phase"] == 2
    assert gc_summary(GC_LOG_DONE) == {"removed": "41.2 GiB", "pending": "12 GiB"}


# -- the queue -----------------------------------------------------------------------------


def _with_cleanup(session: Session, topo: dict[str, Any], when: CleanupWhen) -> None:
    _cleanup(session, prune=False)
    topo["policy"].cleanup_policy = "nightly-cleanup"
    topo["policy"].cleanup_when = when
    session.add(topo["policy"])
    session.commit()


def _sched(world: World, pbs: Any) -> Any:
    sched = _scheduler(world)
    sched._adapters.pbs = pbs
    sched._pipeline_factory = lambda: _pipe(world, pbs)  # type: ignore[method-assign]
    return sched


async def _until(cond: Any) -> None:
    for _ in range(2000):
        if cond():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("timed out waiting")


def _cleanup_runs(session: Session) -> list[PipelineRun]:
    session.expire_all()
    return list(
        session.exec(select(PipelineRun).where(PipelineRun.kind == PipelineKind.CLEANUP)).all()
    )


async def test_a_backup_policy_runs_its_cleanup_before_in_one_wake(
    session: Session, topo: dict[str, Any]
) -> None:
    _with_cleanup(session, topo, CleanupWhen.BEFORE)
    world = _world(topo)
    pbs = CleanPbs(world, polls=1)

    outcomes = await _sched(world, pbs).run_policy("nightly")

    assert {o["backup"] for o in outcomes.values()} == {"succeeded"}
    (run,) = _cleanup_runs(session)
    assert run.status is PipelineStatus.SUCCEEDED
    assert world.index("gc") < world.index("vzdump:101")
    assert world.count("power_on:storage") == 1  # one wake for the cleanup and the backups
    assert world.count("power_off:storage") == 1
    assert world.index("vzdump:103") < world.index("power_off:storage")


async def test_after_runs_the_cleanup_after_the_backups(
    session: Session, topo: dict[str, Any]
) -> None:
    _with_cleanup(session, topo, CleanupWhen.AFTER)
    world = _world(topo)
    pbs = CleanPbs(world, polls=0)

    await _sched(world, pbs).run_policy("nightly")

    assert world.index("vzdump:103") < world.index("gc") < world.index("power_off:storage")
    assert world.count("power_on:storage") == 1


async def test_after_still_cleans_first_when_the_datastore_is_too_full(
    session: Session, topo: dict[str, Any]
) -> None:
    _with_cleanup(session, topo, CleanupWhen.AFTER)
    world = _world(topo)
    pbs = CleanPbs(world, polls=0)
    pbs.free = 100 * 1024**2  # 100 MB: under the backup's 1 GiB floor

    await _sched(world, pbs).run_policy("nightly")

    assert world.index("gc") < world.index("vzdump:101")
    (run,) = _cleanup_runs(session)
    assert "too full" in _steps(run)["cleanup"]["detail"]["reason"]


async def test_jobs_queued_behind_each_other_share_one_wake(
    session: Session, topo: dict[str, Any]
) -> None:
    """A cleanup on its own schedule fires while the nightly batch holds the storage: it
    waits in the queue, then runs in the same wake; the node goes off once, at the end."""
    _cleanup(session, prune=False)
    world = _world(topo)
    pbs = CleanPbs(world, polls=0)
    sched = _sched(world, pbs)
    pbs.gate = asyncio.Event()

    backup = asyncio.create_task(sched.run_policy("nightly"))
    await _until(lambda: "pbs_status" in world.events)  # the batch is at its first preflight
    cleanup = asyncio.create_task(sched.run_cleanup("nightly-cleanup"))
    await _until(lambda: len(sched.queue()["jobs"]) == 2)
    assert [(j["kind"], j["state"]) for j in sched.queue()["jobs"]] == [
        ("backup", "running"),
        ("cleanup", "waiting"),
    ]
    assert sched.queue()["held"]["requires"] == ["truenas"]
    pbs.gate.set()
    await asyncio.gather(backup, cleanup)

    assert world.count("power_on:storage") == 1
    assert world.count("power_off:storage") == 1
    assert world.index("vzdump:103") < world.index("gc") < world.index("power_off:storage")
    assert sched.queue() == {"held": None, "jobs": []}


async def test_a_waiting_job_can_be_cancelled(session: Session, topo: dict[str, Any]) -> None:
    _cleanup(session, prune=False)
    world = _world(topo)
    pbs = CleanPbs(world, polls=0)
    sched = _sched(world, pbs)
    pbs.gate = asyncio.Event()

    backup = asyncio.create_task(sched.run_policy("nightly"))
    await _until(lambda: "pbs_status" in world.events)
    cleanup = asyncio.create_task(sched.run_cleanup("nightly-cleanup"))
    await _until(lambda: len(sched.queue()["jobs"]) == 2)
    waiting = next(j for j in sched.queue()["jobs"] if j["state"] == "waiting")
    assert sched.cancel(waiting["id"]) == "cancelled"
    pbs.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    await backup

    assert "gc" not in world.events
    assert _cleanup_runs(session) == []
    assert world.count("power_off:storage") == 1


async def test_a_job_cancelled_after_the_hand_over_still_lets_the_storage_go(
    session: Session, topo: dict[str, Any]
) -> None:
    """The batch keeps the storage for a job waiting behind it; that job is then cancelled
    before it gets the queue. The storage must still be put back."""
    from orchestrator.scheduler import QueuedJob

    world = _world(topo)
    pbs = CleanPbs(world, polls=0)
    sched = _sched(world, pbs)
    waiting = QueuedJob(
        id=999, kind="cleanup", name="x", actor="t", requires=frozenset({"truenas"})
    )
    sched._jobs.append(waiting)

    await sched.run_policy("nightly")
    assert world.count("power_off:storage") == 0  # kept for the waiting job
    assert sched.queue()["held"] is not None

    sched._jobs.remove(waiting)  # what a cancelled waiting job's own clean-up does
    await sched._release_if_idle()
    assert world.count("power_off:storage") == 1
    assert sched.queue()["held"] is None


async def test_a_cleanup_on_its_own_wakes_and_puts_the_storage_back(
    session: Session, topo: dict[str, Any]
) -> None:
    _cleanup(session, prune=False)
    world = _world(topo)
    pbs = CleanPbs(world, polls=0)

    run_id = await _sched(world, pbs).run_cleanup("nightly-cleanup")

    assert run_id is not None
    assert world.count("power_on:storage") == 1
    assert world.index("gc") < world.index("power_off:storage")


# -- the registry and the API --------------------------------------------------------------


def test_cleanup_policies_round_trip_through_the_yaml(tmp_path: Any) -> None:
    reg = RegistryFile.model_validate(
        {
            "backup_policies": [
                {"name": "nightly", "cleanup_policy": "nightly-cleanup", "cleanup_when": "after"},
                {"name": "weekly"},
            ],
            "cleanup_policies": [
                {"name": "nightly-cleanup", "gc_mode": "above", "gc_above_pct": 85}
            ],
        }
    )
    path = tmp_path / "services.yaml"
    write_yaml_file(path, reg)
    raw = yaml.safe_load(path.read_text())
    weekly = next(p for p in raw["backup_policies"] if p["name"] == "weekly")
    assert "cleanup_policy" not in weekly and "cleanup_when" not in weekly
    parsed, _ = read_yaml_file(path)
    assert parsed.cleanup_policies[0].gc_above_pct == 85
    assert parsed.backup_policies[0].cleanup_when is CleanupWhen.AFTER

    write_yaml_file(path, RegistryFile.model_validate({"backup_policies": [{"name": "w"}]}))
    assert "cleanup_policies" not in yaml.safe_load(path.read_text())


def test_a_backup_policy_naming_an_unknown_cleanup_is_refused() -> None:
    reg = RegistryFile.model_validate(
        {"backup_policies": [{"name": "nightly", "cleanup_policy": "missing"}]}
    )
    with pytest.raises(ValueError, match="runs cleanup policy 'missing'"):
        validate_references(reg)


@pytest.fixture
def app() -> Any:
    from orchestrator.web.app import create_app

    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        node = Node(name="node-a", always_on=True)
        s.add(node)
        s.commit()
        s.refresh(node)
        s.add(Service(slug="storage-vm", name="Storage", node_id=node.id,
                      guest_kind=GuestKind.VM, guest_id=100))  # fmt: skip
        s.add(BackupPolicy(name="nightly"))
        s.commit()
    return create_app()


def test_cleanup_policy_api_crud_links_and_saves(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_auth import _register

    yaml_path = get_settings().services_yaml_path
    with TestClient(app) as client:
        assert client.get("/api/cleanup-policies").status_code == 401
        r = _register(client, monkeypatch, username="admin", credential_id=b"cred-1")
        assert r.status_code == 200, r.text

        body = {"name": "nightly-cleanup", "requires": ["storage-vm"], "gc_interval_h": 20,
                "schedule_cron": "0 4 * * 0"}  # fmt: skip
        r = client.post("/api/cleanup-policies", json=body)
        assert r.status_code == 201, r.text
        assert r.json()["requires"] == ["storage-vm"]
        assert r.json()["next_run"] is not None

        assert client.post("/api/cleanup-policies", json={"name": "x", "gc_mode": "weekly"}
                           ).status_code == 400  # fmt: skip
        assert client.post("/api/cleanup-policies", json={"name": "y", "requires": ["nope"]}
                           ).status_code == 400  # fmt: skip

        r = client.put("/api/backup-policies/nightly",
                       json={"cleanup_policy": "nightly-cleanup", "cleanup_when": "after"})  # fmt: skip
        assert r.status_code == 200, r.text
        assert (r.json()["cleanup_policy"], r.json()["cleanup_when"]) == (
            "nightly-cleanup",
            "after",
        )
        assert client.put("/api/backup-policies/nightly", json={"cleanup_policy": "nope"}
                          ).status_code == 400  # fmt: skip

        got = client.get("/api/cleanup-policies/nightly-cleanup").json()
        assert got["used_by"] == [{"policy": "nightly", "when": "after"}]

        r = client.put("/api/cleanup-policies/nightly-cleanup", json={"gc_mode": "above",
                                                                       "gc_above_pct": 85})  # fmt: skip
        assert r.status_code == 200 and r.json()["gc_mode"] == "above"

        assert client.post("/api/registry/save").status_code == 200
        saved = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert saved["cleanup_policies"][0]["gc_above_pct"] == 85
        assert saved["backup_policies"][0]["cleanup_policy"] == "nightly-cleanup"

        assert client.get("/api/backup-queue").json()["jobs"] == []

        r = client.delete("/api/cleanup-policies/nightly-cleanup")
        assert r.status_code == 200 and r.json()["detached"] == ["nightly"]
        assert client.get("/api/backup-policies/nightly").json()["cleanup_policy"] == ""

    with Session(get_engine()) as s:
        actions = {e.action for e in s.exec(select(AuditEntry)).all()}
    assert {"cleanup_policy.created", "cleanup_policy.updated", "cleanup_policy.deleted"} <= actions


def test_run_cleanup_over_mcp_needs_a_person() -> None:
    from orchestrator.mcp.tools import DANGEROUS, TOOLS

    assert TOOLS["run_cleanup"].risk == DANGEROUS
    assert {"list_cleanup_policies", "backup_queue"} <= set(TOOLS)


def test_stale_entries_in_the_last_gc_lookup_are_per_datastore(
    session: Session, topo: dict[str, Any]
) -> None:
    from orchestrator.pipelines.cleanup import last_gc_at

    done = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    session.add(
        PipelineRun(
            kind=PipelineKind.CLEANUP,
            correlation_id="x",
            status=PipelineStatus.SUCCEEDED,
            steps=[
                {"name": "cleanup", "status": "ok", "detail": {"datastore": "other"}},
                {"name": "gc", "status": "ok", "finished_at": done, "detail": {}},
            ],
        )
    )
    session.commit()
    assert last_gc_at(session, "other") is not None
    assert last_gc_at(session, "example-datastore") is None


# -- part 2: the endpoints behind the screens -----------------------------------------------


@pytest.fixture
def pbs_app() -> Any:
    from orchestrator.web.app import create_app

    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        node = Node(name="node-a", always_on=True)
        s.add(node)
        s.commit()
        s.refresh(node)
        pol = BackupPolicy(name="nightly", retention={"keep_daily": 7})
        s.add(pol)
        s.commit()
        s.refresh(pol)
        s.add(Service(slug="app", name="App", node_id=node.id, guest_kind=GuestKind.VM,
                      guest_id=101, backup_policy_id=pol.id))  # fmt: skip
        s.add(CleanupPolicy(name="nightly-cleanup"))
        s.commit()
    return create_app()


def _snaps() -> list[Snapshot]:
    def snap(t: int, *, verified: bool = False, protected: bool = False) -> Snapshot:
        return Snapshot(datastore="example-datastore", backup_type="vm", backup_id="101",
                        backup_time=t, verified=verified, protected=protected)  # fmt: skip

    return [
        snap(1_790_000_000, verified=True),  # old, verified
        snap(1_790_100_000, protected=True),  # pinned in PBS
        snap(1_790_200_000, verified=True),  # the newest verified: never offered
        snap(1_790_300_000),  # newest, not verified
    ]


def _login(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_auth import _register

    r = _register(client, monkeypatch, username="admin", credential_id=b"cred-1")
    assert r.status_code == 200, r.text


def test_snapshots_say_which_service_and_which_may_be_deleted(
    pbs_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(pbs_app) as client:
        _login(client, monkeypatch)
        pbs = client.app.state.adapters.pbs  # type: ignore[attr-defined]
        pbs.simulated_snapshots = _snaps()
        pbs.synthesize_snapshots = False
        rows = {r["snapshot_id"]: r for r in client.get("/api/infra/pbs/snapshots").json()}
        ids = [s.snapshot_id for s in _snaps()]
        assert all(r["service"] == "app" for r in rows.values())
        assert rows[ids[0]]["keep_reason"] is None
        assert "protected" in rows[ids[1]]["keep_reason"]
        assert "newest verified" in rows[ids[2]]["keep_reason"]
        assert rows[ids[2]]["newest_verified"] is True

        r = client.post("/api/infra/pbs/snapshots/delete", json={"snapshot_ids": ids})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["deleted"] == [ids[0], ids[3]]
        assert set(body["refused"]) == {ids[1], ids[2]}
        assert {s.snapshot_id for s in pbs.simulated_snapshots} == {ids[1], ids[2]}
        assert client.post("/api/infra/pbs/snapshots/delete", json={"snapshot_ids": []}
                           ).status_code == 400  # fmt: skip

    with Session(get_engine()) as s:
        entry = s.exec(
            select(AuditEntry).where(AuditEntry.action == "backup.snapshots_deleted")
        ).one()
        assert entry.actor == "admin" and len(entry.details["deleted"]) == 2


def test_prune_preview_and_prune_now(pbs_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(pbs_app) as client:
        _login(client, monkeypatch)
        r = client.post("/api/backup-policies/nightly/prune-preview")
        assert r.status_code == 200, r.text
        assert [x["service"] for x in r.json()["services"]] == ["app"]
        assert r.json()["total"] == 0  # the dry-run PBS removes nothing

        scheduler = client.app.state.scheduler  # type: ignore[attr-defined]
        called: list[tuple[str, bool]] = []

        async def fake_prune(name: str, *, gc: bool, actor: str = "") -> int:
            called.append((name, gc))
            return 1

        scheduler.run_prune = fake_prune
        r = client.post("/api/backup-policies/nightly/prune", json={"gc": False})
        assert r.status_code == 202, r.text
        assert client.post("/api/backup-policies/missing/prune", json={}).status_code == 404
    assert called == [("nightly", False)]


def test_the_datastore_summary_for_the_cleanup_tab(
    pbs_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(pbs_app) as client:
        _login(client, monkeypatch)
        client.put("/api/backup-policies/nightly", json={"cleanup_policy": "nightly-cleanup"})
        d = client.get("/api/pbs-datastore").json()
        assert d["name"] == get_settings().pbs_datastore
        assert d["cleaned_by"][0]["policy"] == "nightly-cleanup"
        assert d["cleaned_by"][0]["used_by"] == [{"policy": "nightly", "when": "before"}]
        assert "permissions" in d and "own_jobs" in d

        view = client.get("/api/views/backups").json()
        assert [c["name"] for c in view["cleanups"]] == ["nightly-cleanup"]
        assert view["datastore_default"] == get_settings().pbs_datastore
        page = client.get("/backup-policies")
        assert page.status_code == 200 and 'data-tab="cleanup"' in page.text


async def test_prune_now_prunes_only_that_policy_and_can_skip_gc(
    session: Session, topo: dict[str, Any]
) -> None:
    other = BackupPolicy(name="other", retention={"keep_daily": 1})
    session.add(other)
    session.commit()
    session.refresh(other)
    topo["db"].backup_policy_id = other.id
    topo["policy"].retention = {"keep_daily": 7}
    for row in (topo["db"], topo["policy"]):
        session.add(row)
    session.commit()
    world = _world(topo)
    pbs = CleanPbs(world, polls=0)

    run_id = await _sched(world, pbs).run_prune("nightly", gc=False)

    run = session.get(PipelineRun, run_id)
    assert run is not None and run.status is PipelineStatus.SUCCEEDED
    assert {g for g, _ in pbs.pruned} == {"vm/101", "vm/103"}  # not db (vm/102): other policy
    assert _steps(run)["gc"]["status"] == "skipped"
    assert "gc" not in world.events
    assert world.count("power_off:storage") == 1
