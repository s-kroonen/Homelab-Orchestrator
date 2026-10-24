"""Updating from the dashboard: asking the updater, reading its status, announcing the result."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.notify import hooks
from orchestrator.system import selfupdate as su
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin


def _dir() -> Path:
    d = Path(get_settings().state_dir) / "update"
    d.mkdir(parents=True, exist_ok=True)
    return d


def beat(age_s: float = 0) -> None:
    (_dir() / "heartbeat").write_text(f"{int(time.time() - age_s)} 1\n")


def write_status(**fields: Any) -> None:
    base = {
        "id": "run1",
        "action": "update",
        "state": "succeeded",
        "step": "verify",
        "message": "Updated from aaa to bbb.",
        "reason": "",
        "backup": "orchestrator-20261024-120000-pre-update.tar.gz",
        "from_image": "aaaaaaaaaaaa",
        "to_image": "bbbbbbbbbbbb",
        "updated_at": datetime.now(UTC).isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "steps": [{"name": "pull", "result": "OK", "detail": "", "at": ""}],
    }
    (_dir() / "status.json").write_text(json.dumps({**base, **fields}))


def test_the_updater_counts_as_online_only_with_a_fresh_heartbeat_or_a_live_run() -> None:
    settings = get_settings()
    assert su.updater_online(settings) is False and su.heartbeat_age_s(settings) is None
    beat()
    assert su.updater_online(settings) is True
    beat(age_s=120)
    assert su.updater_online(settings) is False  # gone quiet
    write_status(state="running")  # busy updating: no beats, but its status is live
    assert su.updater_online(settings) is True
    write_status(state="running", updated_at="2020-01-01T00:00:00+00:00")
    assert su.updater_online(settings) is False  # a "run" nobody has touched for years


def test_a_request_is_a_small_validated_file_never_a_command() -> None:
    settings = get_settings()
    with pytest.raises(su.UpdaterError, match="not running"):
        su.request_update(settings, by="storm", backup=None)
    beat()

    rid = su.request_update(
        settings, by="storm", backup="orchestrator-20261024-120000-pre-update.tar.gz"
    )

    body = (_dir() / "request").read_text()
    assert f"ID={rid}" in body and "ACTION=update" in body and "BY=storm" in body
    assert "BACKUP=orchestrator-20261024-120000-pre-update.tar.gz" in body
    assert "TAG=" not in body  # none asked for
    with pytest.raises(su.UpdaterError, match="already in progress"):
        su.request_update(settings, by="storm", backup=None)  # one at a time


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"tag": "main; rm -rf /"}, "valid image tag"),
        ({"tag": "evil/other:image"}, "valid image tag"),
        ({"backup": "../../etc/passwd"}, "backup file name"),
    ],
)
def test_bad_fields_are_refused_before_anything_is_written(
    kwargs: dict[str, str], message: str
) -> None:
    beat()
    with pytest.raises(su.UpdaterError, match=message):
        su.request_update(get_settings(), by="storm", **{"backup": None, **kwargs})  # type: ignore[arg-type]
    assert not (_dir() / "request").exists()


def test_an_odd_username_is_replaced_not_passed_on() -> None:
    beat()
    su.request_update(get_settings(), by="a b\nACTION=rollback", backup=None)
    assert "BY=dashboard" in (_dir() / "request").read_text()
    assert (_dir() / "request").read_text().count("ACTION=") == 1


# -- the API -------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    session_module.create_all_for_tests()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_update_goes_to_the_updater_with_a_backup_to_restore(client: TestClient) -> None:
    beat()

    r = client.post("/api/system/update").json()

    assert r["ran"] and r["ok"] and r["method"] == "updater" and r["request_id"]
    assert "puts the old version back" in r["message"]
    backup = r["backup"]["name"]
    assert backup.endswith("-pre-update.tar.gz")
    body = (_dir() / "request").read_text()
    assert f"BACKUP={backup}" in body and "BY=" in body
    # a second press while it is queued is refused, not queued again
    again = client.post("/api/system/update").json()
    assert again["ok"] is False and "already in progress" in again["message"]


def test_without_an_updater_or_a_command_it_says_how_to_set_one_up(client: TestClient) -> None:
    info = client.get("/api/system/update").json()
    assert info["method"] == "manual" and info["configured"] is False
    assert info["updater"]["online"] is False
    r = client.post("/api/system/update").json()
    assert r["ran"] is False and "--profile updater" in r["message"] and r["manual_steps"]


def test_the_check_reports_the_updater_and_the_last_run(client: TestClient) -> None:
    beat()
    write_status()

    info = client.get("/api/system/update").json()

    assert info["method"] == "updater" and info["configured"] is True
    assert info["updater"]["online"] and info["updater"]["status"]["state"] == "succeeded"
    status = client.get("/api/system/update/status").json()
    assert status["status"]["steps"][0]["name"] == "pull" and status["running"] is False


def test_update_log_is_the_tail(client: TestClient) -> None:
    (_dir() / "log").write_text("\n".join(f"line {i}" for i in range(200)))
    log = client.get("/api/system/update/log").json()["log"].splitlines()
    assert log[-1] == "line 199" and len(log) == 80


def test_rollback_backs_up_first_and_asks_for_the_data_from_before_the_update(
    client: TestClient,
) -> None:
    beat()
    write_status(backup="orchestrator-20261024-120000-pre-update.tar.gz")

    r = client.post("/api/system/update/rollback", json={})

    assert r.status_code == 200, r.text
    out = r.json()
    assert out["safety_backup"].endswith("-pre-rollback.tar.gz")
    assert out["restores"] == "orchestrator-20261024-120000-pre-update.tar.gz"
    body = (_dir() / "request").read_text()
    assert (
        "ACTION=rollback" in body
        and "BACKUP=orchestrator-20261024-120000-pre-update.tar.gz" in body
    )
    actions = [e["action"] for e in client.get("/api/activity?kind=admin").json()]
    assert "system.rollback_requested" in actions


def test_rollback_needs_the_updater(client: TestClient) -> None:
    r = client.post("/api/system/update/rollback", json={})
    assert r.status_code == 409 and "not running" in r.json()["detail"]


def test_the_update_api_needs_a_login() -> None:
    session_module.create_all_for_tests()
    with TestClient(create_app()) as anon:
        assert anon.get("/api/system/update/status").status_code == 401
        assert anon.post("/api/system/update/rollback", json={}).status_code == 401


# -- telling people how it went --------------------------------------------------


class Announcements:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, session: Any, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
def announced() -> Any:
    session_module.create_all_for_tests()
    stub = Announcements()
    hooks.install(stub)  # type: ignore[arg-type]
    yield stub
    hooks.install(None)


async def test_a_finished_update_is_announced_once(announced: Announcements) -> None:
    write_status(id="r1", state="succeeded")

    await su.announce_last_result()
    await su.announce_last_result()  # restart again: not announced twice

    assert [e.event for e in announced.events] == ["update_succeeded"]
    assert announced.events[0].severity == "info"

    write_status(id="r2", state="rolled_back", reason="its health check reports unhealthy")
    await su.announce_last_result()
    event = announced.events[-1]
    assert event.event == "update_rolled_back" and event.severity == "error"
    assert "unhealthy" in event.body


async def test_nothing_is_announced_for_a_no_op_or_a_run_in_progress(
    announced: Announcements,
) -> None:
    write_status(id="n1", state="succeeded", from_image="same", to_image="same")
    await su.announce_last_result()
    write_status(id="n2", state="running")
    await su.announce_last_result()
    assert announced.events == []
