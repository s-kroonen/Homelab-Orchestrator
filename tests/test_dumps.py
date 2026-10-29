"""Dumps: a database backed up on its own through Portainer's exec."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.portainer import parse
from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus, Guest
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, DumpRecord, PipelineRun, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind, PipelineStatus
from orchestrator.dumps import policy
from orchestrator.dumps.pipeline import DumpFailed, verify_file
from orchestrator.registry.loader import reconcile_yaml_into_db
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin

REGISTRY = """
version: 1
nodes:
  - {name: pve1, always_on: true}
services:
  - slug: portainer-vm
    name: Portainer VM
    node: pve1
    guest_kind: vm
    guest_id: 101
  - slug: dev-xymbols
    name: dev.xymbols
    parent: portainer-vm
    controller: {type: portainer, endpoint: 1, stack: dev-xymbols}
    dump: {type: postgres, database: app, keep: 2, container: dev-xymbols-db-1}
  - slug: no-dump
    name: nothing
    parent: portainer-vm
"""

PG_OK = b"-- dump\nCREATE TABLE t (id int);\n-- PostgreSQL database dump complete\n"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY)
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    monkeypatch.setenv("DUMP_DIR", str(tmp_path / "dumps"))
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()
    with TestClient(create_app()) as c:
        c.app.state.adapters.proxmox.simulated_guests = [  # type: ignore[attr-defined]
            Guest(node="pve1", vmid=101, kind=GuestKind.VM, name="p", status="running")
        ]
        c.app.state.adapters.portainer.exec_output = PG_OK  # type: ignore[attr-defined]
        _login_as_first_admin(c, monkeypatch)
        yield c


def _portainer(c: TestClient) -> Any:
    return c.app.state.adapters.portainer  # type: ignore[attr-defined]


def _run(c: TestClient, slug: str = "dev-xymbols") -> dict[str, Any]:
    r = c.post(f"/api/services/{slug}/dump/run")
    assert r.status_code == 202, r.text
    import time

    run_id = r.json()["run_id"]
    for _ in range(100):
        got = c.get(f"/api/runs/{run_id}").json()
        if got["status"] != "running":
            return got  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError("the dump did not finish")


# -- config ---------------------------------------------------------------------------------------


def test_normalize_fills_defaults_and_rejects_nonsense() -> None:
    cfg = policy.normalize_dump({"type": "postgres", "database": "app"})
    assert cfg["keep"] == 7 and cfg["schedule"] == "" and cfg["enabled"] is True
    assert policy.normalize_dump({}) == {}
    for bad in (
        {"type": "oracle"},
        {"type": "postgres", "database": "a; rm -rf /"},
        {"type": "sqlite"},
        {"type": "command"},
        {"type": "postgres", "command": "x"},
        {"type": "postgres", "keep": 0},
        {"type": "postgres", "schedule": "not cron"},
        {"type": "postgres", "surprise": 1},
        {"type": "sqlite", "path": "relative/db"},
    ):
        with pytest.raises(ValueError):
            policy.normalize_dump(bad)


def test_the_command_uses_the_containers_own_credentials() -> None:
    pg = policy.build_command(policy.normalize_dump({"type": "postgres", "database": "app"}))
    assert pg[:2] == ["sh", "-c"] and "pg_dump" in pg[2] and "${POSTGRES_USER:-postgres}" in pg[2]
    assert "pg_dumpall" in policy.build_command(policy.normalize_dump({"type": "postgres"}))[2]
    my = policy.build_command(policy.normalize_dump({"type": "mariadb"}))[2]
    assert "MYSQL_ROOT_PASSWORD" in my and "mariadb-dump" in my and "--all-databases" in my
    assert "mongodump" in policy.build_command(policy.normalize_dump({"type": "mongo"}))[2]
    lite = policy.build_command(policy.normalize_dump({"type": "sqlite", "path": "/data/a.db"}))
    assert "sqlite3" in lite[2] and "/data/a.db" in lite[2]
    cmd = policy.build_command(policy.normalize_dump({"type": "command", "command": "tar c /x"}))
    assert cmd == ["sh", "-c", "tar c /x"]


def test_a_dump_needs_a_container_to_run_in() -> None:
    cfg = policy.normalize_dump({"type": "postgres"})
    with pytest.raises(ValueError):
        policy.check_target(cfg, {})
    with pytest.raises(ValueError):
        policy.check_target(cfg, {"type": "portainer", "endpoint": 1, "stack": "s"})
    policy.check_target(cfg, {"type": "portainer", "endpoint": 1, "container": "db"})
    named = policy.normalize_dump({"type": "postgres", "container": "db"})
    policy.check_target(named, {"type": "portainer", "endpoint": 1, "stack": "s"})


def test_frames_are_decoded_across_chunk_boundaries() -> None:
    def frame(kind: int, data: bytes) -> bytes:
        return bytes([kind, 0, 0, 0]) + len(data).to_bytes(4, "big") + data

    wire = frame(1, b"hello ") + frame(2, b"warn") + frame(1, b"world")
    demux = parse.FrameDemux()
    got: list[tuple[str, bytes]] = []
    for i in range(0, len(wire), 5):  # awkward chunks
        got += demux.feed(wire[i : i + 5])
    got += demux.finish()
    assert b"".join(p for s, p in got if s == "stdout") == b"hello world"
    assert b"".join(p for s, p in got if s == "stderr") == b"warn"
    assert parse.FrameDemux().feed(b"plain text, no frames") == [
        ("stdout", b"plain text, no frames")
    ]


# -- a dump ---------------------------------------------------------------------------------------


def test_a_dump_is_compressed_verified_and_recorded(client: TestClient, tmp_path: Path) -> None:
    run = _run(client)
    assert run["status"] == "succeeded", run
    assert [s["name"] for s in run["steps"]] == ["check", "dump", "verify", "record", "prune"]
    info = client.get("/api/services/dev-xymbols/dump").json()
    rec = info["records"][0]
    assert rec["verified"] and rec["size_bytes"] > 0 and rec["file"].endswith(".sql.gz")
    path = tmp_path / "dumps" / rec["path"]
    assert gzip.decompress(path.read_bytes()) == PG_OK
    assert not list((tmp_path / "dumps").rglob("*.part"))
    assert any("exec dev-xymbols-db-1" in c and "pg_dump" in c for c in _portainer(client).calls)
    assert "docker exec -i" in info["restore_hint"] and rec["file"] in info["restore_hint"]
    dl = client.get(f"/api/dumps/{rec['id']}/download")
    assert dl.status_code == 200 and gzip.decompress(dl.content) == PG_OK


def test_a_failing_tool_is_a_failed_dump_with_its_own_message(
    client: TestClient, tmp_path: Path
) -> None:
    p = _portainer(client)
    p.exec_exit, p.exec_stderr, p.exec_output = 1, "pg_dump: error: connection refused", b"x"
    run = _run(client)
    assert run["status"] == "failed" and "connection refused" in run["error"]
    assert client.get("/api/services/dev-xymbols/dump").json()["records"] == []
    assert not list((tmp_path / "dumps").rglob("*.gz*"))  # nothing half-written is left


def test_a_dump_cut_off_half_way_is_not_a_backup(client: TestClient) -> None:
    _portainer(client).exec_output = b"-- dump\nCREATE TABLE t (id int);\n"  # no end marker
    run = _run(client)
    assert run["status"] == "failed" and "cut off" in run["error"]
    assert client.get("/api/services/dev-xymbols/dump").json()["records"] == []


def test_verify_catches_corruption_and_tiny_files(tmp_path: Path) -> None:
    good = tmp_path / "a.gz"
    good.write_bytes(gzip.compress(PG_OK))
    assert verify_file(good, "postgres", 1).uncompressed == len(PG_OK)
    broken = tmp_path / "b.gz"
    broken.write_bytes(good.read_bytes()[:-12])
    with pytest.raises(DumpFailed):
        verify_file(broken, "postgres", 1)
    with pytest.raises(DumpFailed):
        verify_file(good, "postgres", 10_000)


def test_nothing_is_dumped_from_a_node_that_is_off(client: TestClient) -> None:
    pve = client.app.state.adapters.proxmox  # type: ignore[attr-defined]
    pve.simulated_cluster = ClusterStatus(nodes=[ClusterNode(name="pve1", online=False)])
    run = _run(client)
    assert run["status"] == "failed" and "offline" in run["error"]
    assert not any(c.startswith("exec") for c in _portainer(client).calls)


def test_a_stopped_container_is_refused_clearly(client: TestClient) -> None:
    import asyncio

    asyncio.run(_portainer(client).container_action(1, "dev-xymbols-db-1", "stop"))
    run = _run(client)
    assert run["status"] == "failed" and "not running" in run["error"]


def test_read_only_portainer_dumps_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _portainer(client)._read_only = True
    run = _run(client)
    assert run["status"] == "failed" and "read-only" in run["error"]


def test_refusals(client: TestClient) -> None:
    assert client.post("/api/services/ghost/dump/run").status_code == 404
    assert client.post("/api/services/no-dump/dump/run").status_code == 404
    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.slug == "dev-xymbols")).one()
        s.add(
            PipelineRun(
                kind=_dump_kind(),
                correlation_id="c1",
                service_id=svc.id,
                status=PipelineStatus.RUNNING,
            )
        )
        s.commit()
    assert client.post("/api/services/dev-xymbols/dump/run").status_code == 409


def _dump_kind() -> Any:
    from orchestrator.domain.enums import PipelineKind

    return PipelineKind.DUMP


# -- retention ------------------------------------------------------------------------------------


def test_only_the_newest_few_are_kept_but_protected_ones_stay(client: TestClient) -> None:
    first = _run(client)
    assert first["status"] == "succeeded"
    oldest = client.get("/api/services/dev-xymbols/dump").json()["records"][0]
    assert client.post(f"/api/dumps/{oldest['id']}/protect", json={"protected": True}).json()[
        "protected"
    ]
    for _ in range(3):
        assert _run(client)["status"] == "succeeded"
    ids = [r["id"] for r in client.get("/api/services/dev-xymbols/dump").json()["records"]]
    assert oldest["id"] in ids and len(ids) == 3  # keep 2 + the protected one
    assert client.delete(f"/api/dumps/{oldest['id']}").status_code == 409
    client.post(f"/api/dumps/{oldest['id']}/protect", json={"protected": False})
    assert client.delete(f"/api/dumps/{oldest['id']}").status_code == 200
    assert client.get(f"/api/dumps/{oldest['id']}/download").status_code == 404


def test_old_dumps_go_by_age_but_the_newest_good_one_never_does(
    client: TestClient, tmp_path: Path
) -> None:
    from orchestrator.dumps.pipeline import prune

    assert _run(client)["status"] == "succeeded"
    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.slug == "dev-xymbols")).one()
        row = s.exec(select(DumpRecord)).one()
        row.created_at = datetime.now(UTC) - timedelta(days=90)
        s.add(row)
        s.commit()
        removed = prune(s, config_module.get_settings(), svc, {"keep": 7, "keep_days": 30})
        assert removed == []  # the only good dump stays, however old


def test_a_path_outside_the_dump_directory_is_never_served(client: TestClient) -> None:
    assert _run(client)["status"] == "succeeded"
    with Session(get_engine()) as s:
        row = s.exec(select(DumpRecord)).one()
        row.path = "../../etc/passwd"
        s.add(row)
        s.commit()
        rid = row.id
    assert client.get(f"/api/dumps/{rid}/download").status_code == 400


# -- config through the API and registry ----------------------------------------------------------


def test_dump_config_is_validated_when_saved(client: TestClient) -> None:
    ok = client.put(
        "/api/registry/services/dev-xymbols",
        json={
            "dump": {"type": "mariadb", "schedule": "0 3 * * *", "container": "dev-xymbols-db-1"}
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["dump"]["type"] == "mariadb" and ok.json()["dump"]["keep"] == 7
    assert (
        client.put("/api/registry/services/dev-xymbols", json={"dump": {"type": "x"}}).status_code
        == 400
    )
    # a service with no container to run in cannot have one
    assert (
        client.put(
            "/api/registry/services/no-dump", json={"dump": {"type": "postgres"}}
        ).status_code
        == 400
    )
    overview = client.get("/api/dumps").json()
    assert [o["service"] for o in overview] == ["dev-xymbols"] and overview[0][
        "schedule"
    ] == "0 3 * * *"


def test_the_schedule_becomes_a_job(client: TestClient) -> None:
    client.put(
        "/api/registry/services/dev-xymbols",
        json={
            "dump": {"type": "postgres", "schedule": "0 3 * * *", "container": "dev-xymbols-db-1"}
        },
    )
    sched = client.app.state.scheduler  # type: ignore[attr-defined]
    if sched is None:
        pytest.skip("scheduler disabled in this environment")
    sched.sync_dump_jobs()
    assert sched._scheduler.get_job("dump:dev-xymbols") is not None
    client.put(
        "/api/registry/services/dev-xymbols",
        json={"dump": {"type": "postgres", "container": "dev-xymbols-db-1"}},
    )
    sched.sync_dump_jobs()
    assert sched._scheduler.get_job("dump:dev-xymbols") is None


def test_a_dump_is_audited(client: TestClient) -> None:
    assert _run(client)["status"] == "succeeded"
    with Session(get_engine()) as s:
        actions = [a.action for a in s.exec(select(AuditEntry)).all()]
    assert "dump.start" in actions and "dump.done" in actions


# -- the shell and the assistant -----------------------------------------------------------------


def test_cli_sets_shows_and_clears_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from orchestrator.cli import cli

    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY)
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    config_module.reset_settings_cache()
    runner = CliRunner()
    out = runner.invoke(
        cli, ["dump", "set", "dev-xymbols", "--schedule", "0 3 * * *", "--keep", "4"]
    )
    assert out.exit_code == 0, out.output
    assert "0 3 * * *" in out.output and "newest 4" in out.output
    assert "postgres" in runner.invoke(cli, ["dump", "show", "dev-xymbols"]).output
    bad = runner.invoke(cli, ["dump", "set", "no-dump", "--type", "postgres"])
    assert bad.exit_code != 0 and "container" in bad.output
    assert runner.invoke(cli, ["dump", "clear", "dev-xymbols"]).exit_code == 0
    assert "no dump" in runner.invoke(cli, ["dump", "show", "dev-xymbols"]).output


def test_cli_runs_and_lists(client: TestClient) -> None:
    from click.testing import CliRunner

    from orchestrator.cli import cli

    runner = CliRunner()
    done = runner.invoke(cli, ["dump", "run", "dev-xymbols"])
    assert done.exit_code == 0, done.output
    assert "succeeded" in done.output
    listed = runner.invoke(cli, ["dump", "list", "dev-xymbols"])
    assert "dev-xymbols" in listed.output and ".sql.gz" in listed.output


def test_an_assistant_can_look_and_start_a_dump_but_only_with_the_backup_permission(
    client: TestClient,
) -> None:
    made = client.post("/api/assistants/grants", json={"label": "ci", "scopes": ["read"]}).json()
    token = made["token"]

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        r = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
            headers={"authorization": f"Bearer {token}"},
        )
        return r.json()["result"]  # type: ignore[no-any-return]

    assert call("list_dumps", {})["isError"] is False
    assert call("run_dump", {"slug": "dev-xymbols"})["isError"] is True  # needs backup
    gid = client.get("/api/assistants/grants").json()[0]["id"]
    client.patch(f"/api/assistants/grants/{gid}", json={"scopes": ["read", "backup"]})
    out = call("run_dump", {"slug": "dev-xymbols"})
    assert out["isError"] is False, out
    assert "run_id" in out["content"][0]["text"]
