"""A stack is one sub-service: its containers are checks on it (``registry/stack_fold.py``), and
a backup can wait for a single check (``Probe.backup_waits``)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import Session, select

from orchestrator.config import get_settings
from orchestrator.db.models import (
    AuditEntry,
    BackupPolicy,
    Node,
    Probe,
    ProxyHost,
    Service,
)
from orchestrator.domain.enums import BackupGate, GuestKind, HealthState, ProbeKind
from orchestrator.domain.schemas import ProbeResult, ServiceVerdict
from orchestrator.pipelines.backup import BackupPipeline, BackupSkipped
from orchestrator.pipelines.steps import StepRecorder
from orchestrator.registry import stack_fold

LINK = {"type": "portainer", "endpoint": 3}


def _tree(session: Session) -> dict[str, Service]:
    node = Node(name="node-a", power_mgr_target="node-a")
    session.add(node)
    session.commit()
    host = Service(
        slug="docker-host",
        name="Docker host",
        node_id=node.id,
        guest_kind=GuestKind.VM,
        guest_id=102,
    )
    session.add(host)
    session.commit()
    stack = Service(
        slug="shop", name="Shop", parent_id=host.id, guest_kind=GuestKind.NONE,
        controller={**LINK, "stack": "shop"},
    )  # fmt: skip
    session.add(stack)
    session.commit()
    kids = {}
    for name, gate in (("shop-db", BackupGate.REQUIRED), ("shop-web", BackupGate.IGNORE)):
        kid = Service(
            slug=name, name=name, parent_id=stack.id, guest_kind=GuestKind.NONE,
            controller={**LINK, "container": name}, backup_gate=gate,
        )  # fmt: skip
        session.add(kid)
        session.commit()
        session.add(
            Probe(service_id=kid.id, name="portainer", kind=ProbeKind.PORTAINER,
                  required=True, config={"endpoint": 3, "container": name})
        )  # fmt: skip
        kids[name] = kid
    session.add(Probe(service_id=stack.id, name="portainer", kind=ProbeKind.PORTAINER,
                      config={"endpoint": 3, "stack": "shop"}))  # fmt: skip
    session.add(ProxyHost(hostname="db-admin.example.lan", service_id=kids["shop-db"].id))
    other = Service(slug="reports", name="Reports", depends_on=["shop-db"])
    session.add(other)
    session.add(BackupPolicy(name="nightly", requires=["shop-web", "docker-host"]))
    session.commit()
    return {"host": host, "stack": stack, **kids}


def test_folding_turns_container_children_into_checks_on_the_stack(session: Session) -> None:
    t = _tree(session)

    done = stack_fold.fold_all(session)

    assert [d.stack for d in done] == ["shop"]
    assert {s.slug for s in session.exec(select(Service)).all()} == {
        "docker-host",
        "shop",
        "reports",
    }
    probes = {p.name: p for p in session.exec(select(Probe)).all()}
    assert set(probes) == {"portainer", "container:shop-db", "container:shop-web"}
    db, web = probes["container:shop-db"], probes["container:shop-web"]
    assert db.service_id == t["stack"].id and db.config["container"] == "shop-db"
    assert db.backup_waits is True and web.backup_waits is False  # the gate is kept
    host = session.exec(select(ProxyHost)).one()
    assert host.service_id == t["stack"].id
    reports = session.exec(select(Service).where(Service.slug == "reports")).one()
    assert reports.depends_on == ["shop"]
    pol = session.exec(select(BackupPolicy)).one()
    assert pol.requires == ["shop", "docker-host"]
    assert session.exec(select(AuditEntry).where(AuditEntry.action == "stack.folded")).one()
    assert stack_fold.fold_all(session) == []  # once


def test_a_child_that_is_something_else_stays(session: Session) -> None:
    t = _tree(session)
    t["shop-web"].controller = {"type": "portainer", "endpoint": 9, "container": "shop-web"}
    session.add(t["shop-web"])
    session.commit()

    stack_fold.fold_all(session)

    assert session.exec(select(Service).where(Service.slug == "shop-web")).one()


# -- a backup waits for single checks -------------------------------------------------------------


class _Health:
    def __init__(self, states: dict[str, HealthState]) -> None:
        self.states = states

    def can_verify(self, session: Any, service: Any) -> bool:
        return True

    async def scan(self, session: Any, service: Any) -> ServiceVerdict:
        results = [
            ProbeResult(probe_name=n, kind=ProbeKind.PORTAINER, state=s, message=f"{n} is {s}")
            for n, s in self.states.items()
        ]
        worst = (
            HealthState.FAILED
            if HealthState.FAILED in self.states.values()
            else HealthState.HEALTHY
        )
        return ServiceVerdict(state=worst, reason="test", probe_results=results)


def _pipeline(states: dict[str, HealthState]) -> BackupPipeline:
    from orchestrator.adapters.pbs.dry_run import DryRunPbsAdapter
    from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter

    return BackupPipeline(
        proxmox=DryRunProxmoxAdapter(), pbs=DryRunPbsAdapter(), settings=get_settings(),
        health=_Health(states),  # type: ignore[arg-type]
    )  # fmt: skip


async def _gate(session: Session, states: dict[str, HealthState]) -> list[dict[str, Any]]:
    from orchestrator.db.models import PipelineRun
    from orchestrator.domain.enums import PipelineKind, PipelineStatus

    host = session.exec(select(Service).where(Service.slug == "docker-host")).one()
    run = PipelineRun(
        kind=PipelineKind.BACKUP, status=PipelineStatus.RUNNING, steps=[], correlation_id="t"
    )
    session.add(run)
    session.commit()
    steps = StepRecorder(session, run)
    await _pipeline(states)._run_sub_gate(session, steps, host, gate=True)
    return run.steps


async def test_a_backup_waits_only_for_the_checks_marked_so(session: Session) -> None:
    _tree(session)
    stack_fold.fold_all(session)

    # the web container is down, the database is fine: the backup goes ahead
    steps = await _gate(
        session,
        {"container:shop-db": HealthState.HEALTHY, "container:shop-web": HealthState.FAILED},
    )
    assert steps[-1]["status"] == "ok" and steps[-1]["detail"]["checks"] == ["container:shop-db"]

    with pytest.raises(BackupSkipped) as err:
        await _gate(
            session,
            {"container:shop-db": HealthState.FAILED, "container:shop-web": HealthState.HEALTHY},
        )
    assert err.value.why == "sub_unhealthy" and "container:shop-db" in str(err.value)
    assert err.value.retryable is False


def test_the_backup_plan_names_the_checks_it_waits_for(session: Session) -> None:
    from orchestrator.registry.backup_plan import resolve_backup_plan

    _tree(session)
    stack_fold.fold_all(session)
    host = session.exec(select(Service).where(Service.slug == "docker-host")).one()

    plan = resolve_backup_plan(session, host, None)

    assert plan.required_subs == [
        {
            "slug": "shop",
            "name": "Shop",
            "controller": "portainer",
            "can_start": True,
            "checks": ["container:shop-db"],
        }
    ]


def test_a_containers_dump_moves_to_the_stack_and_one_with_recovery_stays(session: Session) -> None:
    t = _tree(session)
    t["shop-db"].dump = {"type": "postgres", "database": "app"}
    t["shop-web"].recovery = {"enabled": True}
    session.add_all([t["shop-db"], t["shop-web"]])
    session.commit()

    stack_fold.fold_all(session)

    stack = session.exec(select(Service).where(Service.slug == "shop")).one()
    assert stack.dump == {"type": "postgres", "database": "app", "container": "shop-db"}
    left = {s.slug for s in session.exec(select(Service)).all()}
    assert "shop-db" not in left and "shop-web" in left
