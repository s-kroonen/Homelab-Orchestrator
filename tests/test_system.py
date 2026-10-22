"""The orchestrator managing itself: backups, staged restore, schema, update check."""

from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from orchestrator import config as config_module
from orchestrator.cli import cli
from orchestrator.config import Settings, get_settings
from orchestrator.db import session as session_module
from orchestrator.system import backups as bk
from orchestrator.system import info as sysinfo
from orchestrator.web.app import create_app
from tests.test_infra_pages import _login


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    db = tmp_path / "state" / "orchestrator.db"
    db.parent.mkdir()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('original')")
        conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
        conn.execute("INSERT INTO alembic_version VALUES ('0001_baseline')")
    registry = tmp_path / "services.yaml"
    registry.write_text("version: 1\n# original\n")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SERVICES_YAML_PATH", str(registry))
    config_module.reset_settings_cache()
    return get_settings()


def _value(db: Path) -> str:
    with sqlite3.connect(db) as conn:
        return str(conn.execute("SELECT v FROM t").fetchone()[0])


def test_backup_roundtrip_is_listed_and_consistent(settings: Settings) -> None:
    info = bk.create_backup(settings, reason="manual", note="before the upgrade")
    assert info.name.endswith("-manual.tar.gz") and info.has_registry
    assert info.schema_revision == "0001_baseline" and info.note == "before the upgrade"
    assert [b.name for b in bk.list_backups(settings)] == [info.name]
    with tarfile.open(bk.backup_path(settings, info.name)) as tar:
        assert set(tar.getnames()) == {"manifest.json", "orchestrator.db", "services.yaml"}


def test_names_are_validated_against_path_tricks(settings: Settings) -> None:
    for bad in ("../x.tar.gz", "orchestrator-1.tar.gz", "/etc/passwd"):
        with pytest.raises(bk.BackupError):
            bk.backup_path(settings, bad)


def test_staged_restore_is_applied_on_start_with_a_safety_backup(settings: Settings) -> None:
    db = bk.sqlite_path(settings)
    registry = Path(settings.services_yaml_path)
    original = bk.create_backup(settings, reason="manual")

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE t SET v = 'changed'")
    registry.write_text("version: 1\n# changed\n")

    marker = bk.stage_restore(settings, original.name, known_revisions={"0001_baseline"})
    assert marker["backup"] == original.name and bk.pending_restore(settings)
    assert _value(db) == "changed"  # nothing live was touched yet

    assert bk.apply_pending_restore(settings) == original.name
    assert _value(db) == "original" and "# original" in registry.read_text()
    assert bk.pending_restore(settings) is None
    reasons = {b.reason for b in bk.list_backups(settings)}
    assert "pre-restore" in reasons  # the 'changed' state was kept
    assert list(registry.parent.glob("services.yaml.bak-*"))


def test_restore_refuses_a_newer_schema_and_can_be_cancelled(settings: Settings) -> None:
    info = bk.create_backup(settings)
    with pytest.raises(bk.BackupError, match="newer schema"):
        bk.stage_restore(settings, info.name, known_revisions={"0000_other"})
    bk.stage_restore(settings, info.name, known_revisions=set())
    assert bk.cancel_restore(settings) is True and bk.pending_restore(settings) is None
    assert bk.cancel_restore(settings) is False


def test_prune_only_touches_scheduled_backups(settings: Settings) -> None:
    import time

    kept = bk.create_backup(settings, reason="manual")
    for _ in range(3):
        bk.create_backup(settings, reason="scheduled")
        time.sleep(1.05)
    gone = bk.prune_backups(settings, keep=1)
    assert len(gone) == 2
    names = {b.name for b in bk.list_backups(settings)}
    assert kept.name in names and len(names) == 2


def test_image_tag_and_channel(settings: Settings) -> None:
    s = settings.model_copy(update={"orchestrator_image": "ghcr.io/o/r:dev"})
    assert sysinfo.image_tag(s) == "dev" and sysinfo._channel(s) == "dev"
    s = settings.model_copy(update={"orchestrator_image": "localhost:5000/r"})
    assert sysinfo.image_tag(s) == "latest" and sysinfo._channel(s) == "main"


async def test_update_check_compares_commits(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    sysinfo._UPDATE_CACHE.clear()
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/commits/dev")
        return httpx.Response(
            200,
            json={
                "sha": "b" * 40,
                "html_url": "https://x/c",
                "commit": {"message": "Fix a thing\n\nmore"},
            },
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    s = settings.model_copy(
        update={"orchestrator_image": "ghcr.io/o/r:dev", "orchestrator_git_sha": "a" * 40}
    )
    check = await sysinfo.check_update(s, force=True)
    assert check.available is True and "Fix a thing" in check.message
    same = settings.model_copy(
        update={"orchestrator_image": "ghcr.io/o/r:dev", "orchestrator_git_sha": "b" * 40}
    )
    assert (await sysinfo.check_update(same, force=True)).available is False
    unknown = settings.model_copy(update={"orchestrator_image": "ghcr.io/o/r:dev"})
    assert (await sysinfo.check_update(unknown, force=True)).available is None


async def test_update_without_a_command_backs_up_and_lists_manual_steps(
    settings: Settings,
) -> None:
    from orchestrator.system.update import run_update

    result = await run_update(settings)
    assert result.backup and result.backup.reason == "pre-update"
    assert result.ran is False and any("docker compose pull" in s for s in result.manual_steps)


async def test_update_runs_the_configured_command_through_the_transport(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator.health.transports.base import CommandResult
    from orchestrator.system import update as upd

    seen: list[list[str]] = []

    class Fake:
        async def run(self, argv: list[str], *, timeout_s: int, env: Any = None) -> CommandResult:
            seen.append(list(argv))
            return CommandResult(0, "started", "")

    monkeypatch.setattr(upd, "_build_transport", lambda s: Fake())
    s = settings.model_copy(
        update={
            "self_update_command": "sh -c 'docker compose up -d orchestrator'",
            "self_update_transport": "local",
        }
    )
    result = await upd.run_update(s)
    assert result.ran and result.ok
    assert seen == [["sh", "-c", "docker compose up -d orchestrator"]]


# --- web + CLI ---------------------------------------------------------------


def test_api_and_page(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    session_module.create_all_for_tests()
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/api/system/info").status_code == 401  # admin only
        _login(client, monkeypatch)
        page = client.get("/system")
        assert page.status_code == 200 and "Back up now" in page.text
        info = client.get("/api/system/info").json()
        assert info["version"] and "schema" in info

        made = client.post("/api/system/backups", json={"note": "api"})
        assert made.status_code == 201, made.text
        name = made.json()["name"]
        assert [b["name"] for b in client.get("/api/system/backups").json()] == [name]
        dl = client.get(f"/api/system/backups/{name}/download")
        assert dl.status_code == 200 and dl.headers["content-type"] == "application/gzip"
        assert client.get("/api/system/backups/..%2Fx.tar.gz/download").status_code in (400, 404)

        staged = client.post(f"/api/system/backups/{name}/restore")
        assert staged.status_code == 200, staged.text
        assert client.get("/api/system/info").json()["pending_restore"]["backup"] == name
        assert client.delete("/api/system/restore").json() == {"cancelled": True}

        assert client.delete(f"/api/system/backups/{name}").json() == {"deleted": name}
        assert client.delete(f"/api/system/backups/{name}").status_code == 404


def test_restart_schedules_a_stop_and_does_not_kill_the_test(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator.system import update as upd

    called: list[float] = []
    monkeypatch.setattr(upd, "schedule_restart", lambda delay_s=1.0: called.append(delay_s))
    session_module.create_all_for_tests()
    with TestClient(create_app()) as client:
        _login(client, monkeypatch)
        assert client.post("/api/system/restart").json() == {"restarting": True}
    assert called


def test_cli_backup_list_and_restore(settings: Settings) -> None:
    runner = CliRunner()
    out = runner.invoke(cli, ["system", "backup", "--note", "cli"])
    assert out.exit_code == 0, out.output
    listing = runner.invoke(cli, ["system", "backups"]).output
    name = listing.split()[0]
    assert name.startswith("orchestrator-") and "cli" in listing
    staged = runner.invoke(cli, ["system", "restore", name])
    assert staged.exit_code == 0 and "Staged" in staged.output
    assert runner.invoke(cli, ["system", "restore-cancel"]).output.strip() == "Cancelled."
    bad = runner.invoke(cli, ["system", "restore", "nope"])
    assert bad.exit_code != 0


def test_startup_takes_a_backup_before_migrating_an_older_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic import command
    from alembic.config import Config

    db = tmp_path / "old.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SERVICES_YAML_PATH", str(tmp_path / "services.yaml"))
    monkeypatch.setenv("RUN_MIGRATIONS_ON_START", "true")
    config_module.reset_settings_cache()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, "0016_probe_enabled")

    with TestClient(create_app()):
        pass

    settings = get_settings()
    reasons = [b.reason for b in bk.list_backups(settings)]
    assert "pre-migration" in reasons
    assert sysinfo.schema_status(settings)["pending"] is False
    old = next(b for b in bk.list_backups(settings) if b.reason == "pre-migration")
    assert old.schema_revision == "0016_probe_enabled"


def test_a_staged_restore_is_applied_when_the_app_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic import command
    from alembic.config import Config

    db = tmp_path / "live.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SERVICES_YAML_PATH", str(tmp_path / "services.yaml"))
    monkeypatch.setenv("RUN_MIGRATIONS_ON_START", "true")
    config_module.reset_settings_cache()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, "head")
    settings = get_settings()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO node (name, always_on, power_mgr_target, notes, address, wake_on_hit_enabled, created_at, updated_at) VALUES ('keepme',0,'','','',1,'2026-01-01','2026-01-01')"
        )
    snap = bk.create_backup(settings, reason="manual")
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM node")
    bk.stage_restore(settings, snap.name, known_revisions=sysinfo.known_revisions(settings))

    with TestClient(create_app()):
        pass

    with sqlite3.connect(db) as conn:
        assert [r[0] for r in conn.execute("SELECT name FROM node")] == ["keepme"]
    assert bk.pending_restore(settings) is None
