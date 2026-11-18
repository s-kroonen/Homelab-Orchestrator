"""Background jobs that fail (pruning, the orchestrator's own backup, saving services.yaml,
pushing proxy hosts) are warnings with the error, not log lines nobody reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlmodel import Session, select

from orchestrator.adapters.proxmox_family import error_reason
from orchestrator.db.models import NotificationEvent
from orchestrator.notify import hooks
from orchestrator.notify.jobs import job_failed, job_ok, repeat_reason
from orchestrator.notify.links import link_for

PBS_400 = (
    "pbs returned 400 on POST /admin/datastore/store/prune: "
    "parameter verification errors; keep-daily: value must be at least 1"
)


class Recorder:
    """Stands in for the engine: records what would be sent, and keeps the feed."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def emit(self, session: Session, event: Any) -> None:
        held = next((t for t in event.tags if t.startswith("suppressed:")), "")
        session.add(NotificationEvent(level=event.level, target=event.key, event=event.event,
                                      severity=event.severity, title=event.title, body=event.body,
                                      suppressed=bool(held), suppressed_reason=held))  # fmt: skip
        session.commit()
        if not held:
            self.sent.append(event)


@pytest.fixture
def engine() -> Any:
    rec = Recorder()
    hooks.install(rec)  # type: ignore[arg-type]
    yield rec
    hooks.install(None)


# -- the adapter says why ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "want"),
    [
        (
            '{"data":null,"message":"parameter verification errors","errors":{"keep-daily":'
            '"value must be at least 1"}}',
            "parameter verification errors; keep-daily: value must be at least 1",
        ),
        (
            '{"data":null,"errors":{"backup-id":"property is missing"}}',
            "backup-id: property is missing",
        ),
        ("unable to acquire lock", "unable to acquire lock"),
        ("", ""),
    ],
)
def test_error_reason_reads_what_proxmox_and_pbs_say(body: str, want: str) -> None:
    assert error_reason(body) == want


async def test_a_request_error_carries_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from orchestrator.adapters.errors import AdapterRequestError
    from orchestrator.adapters.proxmox_family import ProxmoxFamilyClient

    body = '{"data":null,"errors":{"keep-daily":"value must be at least 1"}}'
    client = ProxmoxFamilyClient.__new__(ProxmoxFamilyClient)
    client._flavor = "pbs"  # type: ignore[attr-defined]

    class Fake:
        async def request(self, *a: Any, **kw: Any) -> httpx.Response:
            return httpx.Response(400, text=body)

    client._client = Fake()  # type: ignore[attr-defined]
    with pytest.raises(AdapterRequestError) as caught:
        await client.request("POST", "/admin/datastore/store/prune", data={"keep-daily": 0})
    assert "keep-daily: value must be at least 1" in str(caught.value)
    assert caught.value.status_code == 400


# -- a failed job is a warning, once -----------------------------------------------------------


async def test_a_failed_job_is_a_warning_with_the_error(session: Session, engine: Recorder) -> None:
    await job_failed(session, job="retention:haos", name="Pruning old backups of haos",
                     error=PBS_400, impact="old snapshots stay")  # fmt: skip
    (sent,) = engine.sent
    assert sent.level == "job" and sent.event == "job_failed" and sent.severity == "warn"
    assert sent.title == "Pruning old backups of haos failed"
    assert "keep-daily: value must be at least 1" in sent.body and "old snapshots stay" in sent.body


async def test_the_same_failure_is_sent_once_a_day(session: Session, engine: Recorder) -> None:
    for _ in range(3):  # three nights in a row, same error
        await job_failed(session, job="retention:haos", name="Pruning", error=PBS_400)
    assert len(engine.sent) == 1
    held = [e for e in session.exec(select(NotificationEvent)).all() if e.suppressed]
    assert len(held) == 2 and "has not worked since" in held[0].suppressed_reason
    # a different error is news
    await job_failed(session, job="retention:haos", name="Pruning", error="pbs timed out")
    assert len(engine.sent) == 2
    # another job is its own
    await job_failed(session, job="retention:portainer", name="Pruning", error=PBS_400)
    assert len(engine.sent) == 3


async def test_after_a_day_it_is_sent_again(session: Session, engine: Recorder) -> None:
    await job_failed(session, job="self_backup", name="Backup", error="disk full")
    row = session.exec(select(NotificationEvent)).one()
    row.at = datetime.now(UTC) - timedelta(hours=25)
    session.add(row)
    session.commit()
    assert repeat_reason(session, "self_backup", row.body) == ""


async def test_working_again_is_announced_once(session: Session, engine: Recorder) -> None:
    await job_ok(session, job="autosave:yaml", name="Saving services.yaml")
    assert engine.sent == []  # never failed: not news
    await job_failed(session, job="autosave:yaml", name="Saving services.yaml", error="read-only")
    await job_ok(session, job="autosave:yaml", name="Saving services.yaml")
    await job_ok(session, job="autosave:yaml", name="Saving services.yaml")
    assert [e.event for e in engine.sent] == ["job_failed", "recovered"]
    assert engine.sent[1].severity == "info" and "read-only" in engine.sent[1].body
    # failing again after it recovered is news again, even with the same error
    await job_failed(session, job="autosave:yaml", name="Saving services.yaml", error="read-only")
    assert [e.event for e in engine.sent][-1] == "job_failed"


def test_job_alerts_open_the_right_page() -> None:
    assert link_for("job", "retention:haos", "job_failed") == "/services/haos#backups"
    assert link_for("job", "autosave:push", "job_failed") == "/system"
    assert link_for("job", "self_backup", "recovered") == "/system"


def test_job_failed_is_sent_by_default() -> None:
    from orchestrator.notify.engine import DEFAULT_EVENTS, EVENTS, LEVELS

    assert "job" in LEVELS and "job_failed" in EVENTS and "job_failed" in DEFAULT_EVENTS
