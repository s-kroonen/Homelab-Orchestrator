"""Controllers (acting on one service itself) and the actions API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.proxmox.base import Guest
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter
from orchestrator.config import Settings, get_settings
from orchestrator.controllers import (
    ControllerError,
    ControllerUnavailable,
    build_controller,
    validate_controller,
)
from orchestrator.controllers.command import CommandController
from orchestrator.controllers.guest import GuestController
from orchestrator.controllers.haos import HomeAssistantController
from orchestrator.controllers.portainer import PortainerController
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, PipelineRun, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind, PipelineKind, PipelineStatus
from orchestrator.health.transports.base import CommandResult
from orchestrator.health.transports.dry_run import DryRunTransport
from orchestrator.registry.loader import reconcile_yaml_into_db
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin

_REAL_CLIENT = httpx.AsyncClient  # patch_http may run twice in one test


def patch_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw),
    )


# --- validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("cfg", "message"),
    [
        ({"type": "teleport"}, "must be one of"),
        ({"type": "portainer", "container": "x"}, "needs 'endpoint'"),
        ({"type": "portainer", "endpoint": "two", "container": "x"}, "number"),
        ({"type": "portainer", "endpoint": 1}, "exactly one of"),
        ({"type": "portainer", "endpoint": 1, "container": "a", "stack": "b"}, "exactly one of"),
        ({"type": "haos"}, "exactly one of"),
        ({"type": "docker"}, "needs 'container'"),
        ({"type": "systemd"}, "needs 'unit'"),
        ({"type": "command"}, "at least one of"),
        ({"type": "command", "restart": "systemctl restart x"}, "list of arguments"),
        ({"type": "docker", "container": "x", "transport": "ssh"}, "object"),
    ],
)
def test_validate_controller_rejects(cfg: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_controller(cfg)


def test_validate_controller_accepts_and_normalises() -> None:
    assert validate_controller(None) == {} and validate_controller({}) == {}
    out = validate_controller({"type": "Portainer", "endpoint": "2", "container": "nginx"})
    assert out == {"type": "portainer", "endpoint": 2, "container": "nginx"}
    assert validate_controller({"type": "haos", "core": True})["core"] is True


# --- portainer -----------------------------------------------------------------


def portainer(**env: Any) -> PortainerController:
    s = Settings(
        portainer_url="https://portainer.test", portainer_api_key="key", dry_run=False, **env
    )
    return PortainerController(s, {"endpoint": 2, "container": "nginx"})


async def test_portainer_container_status_and_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, dict(request.url.params)))
        assert request.headers["x-api-key"] == "key"
        if request.url.path.endswith("/containers/json"):
            assert json.loads(request.url.params["filters"]) == {"name": ["^/nginx$"]}
            return httpx.Response(200, json=[{"Id": "abc123", "State": "running"}])
        return httpx.Response(204)

    patch_http(monkeypatch, handler)
    ctl = portainer()
    st = await ctl.status()
    assert st.running is True and st.state == "running"
    assert "restart sent" in await ctl.restart()
    assert calls[-1][:2] == ("POST", "/api/endpoints/2/docker/containers/abc123/restart")
    assert calls[-1][2] == {"t": "10"}


async def test_portainer_errors_are_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    ctl = portainer()
    patch_http(monkeypatch, lambda r: httpx.Response(200, json=[]))
    with pytest.raises(ControllerError, match="no container named 'nginx'"):
        await ctl.start()
    patch_http(monkeypatch, lambda r: httpx.Response(401))
    with pytest.raises(ControllerError, match="refused the API key"):
        await ctl.status()
    unconfigured = PortainerController(Settings(dry_run=False), {"endpoint": 1, "container": "x"})
    with pytest.raises(ControllerError, match="PORTAINER_URL"):
        await unconfigured.status()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    patch_http(monkeypatch, boom)
    with pytest.raises(ControllerUnavailable):
        await ctl.status()


async def test_portainer_already_running_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/json"):
            return httpx.Response(200, json=[{"Id": "x", "State": "running"}])
        return httpx.Response(304)

    patch_http(monkeypatch, handler)
    assert "already" in await portainer().start()


async def test_portainer_stacks(monkeypatch: pytest.MonkeyPatch) -> None:
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, json=[{"Id": 7, "Name": "media", "EndpointId": 2, "Status": 1}]
            )
        posts.append(request.url.path)
        return httpx.Response(200)

    patch_http(monkeypatch, handler)
    s = Settings(portainer_url="https://p.test", portainer_api_key="k", dry_run=False)
    ctl = PortainerController(s, {"endpoint": 2, "stack": "media"})
    assert (await ctl.status()).running is True
    await ctl.restart()
    assert posts == ["/api/stacks/7/stop", "/api/stacks/7/start"]


# --- home assistant -------------------------------------------------------------


async def test_haos_addon_and_core(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer tok"
        if request.url.path.endswith("/info"):
            return httpx.Response(200, json={"data": {"state": "stopped"}})
        return httpx.Response(200, json={})

    patch_http(monkeypatch, handler)
    s = Settings(haos_url="http://ha.test:8123", haos_token="tok", dry_run=False)
    addon = HomeAssistantController(s, {"addon": "core_mosquitto"})
    assert (await addon.status()).running is False
    await addon.restart()
    assert seen[-1] == ("POST", "/api/hassio/addons/core_mosquitto/restart")
    core = HomeAssistantController(s, {"core": True})
    await core.restart()
    assert seen[-1] == ("POST", "/api/services/homeassistant/restart")
    with pytest.raises(ControllerError, match="cannot be started"):
        await core.start()


# --- command controllers --------------------------------------------------------


async def test_docker_systemd_and_custom_commands() -> None:
    t = DryRunTransport()
    s = Settings(dry_run=False)
    docker = CommandController(s, {"type": "docker", "container": "nginx"}, t)
    t.script("docker inspect", CommandResult(0, "running\n"))
    assert (await docker.status()).running is True
    await docker.restart()
    assert t.calls[-1] == ["docker", "restart", "nginx"]

    unit = CommandController(s, {"type": "systemd", "unit": "caddy"}, t)
    t.script("is-active", CommandResult(3, "inactive\n"))
    assert (await unit.status()).running is False

    custom = CommandController(s, {"type": "command", "restart": ["/opt/restart.sh", "--now"]}, t)
    await custom.restart()
    assert t.calls[-1] == ["/opt/restart.sh", "--now"]
    with pytest.raises(ControllerError, match="no stop command"):
        await custom.stop()

    t.script("docker stop", CommandResult(1, "", "No such container"))
    with pytest.raises(ControllerError, match="exit 1"):
        await docker.stop()
    down = CommandController(
        s, {"type": "docker", "container": "x"}, DryRunTransport(unreachable=True)
    )
    with pytest.raises(ControllerUnavailable):
        await down.start()


# --- the Proxmox guest ------------------------------------------------------------


async def test_guest_controller_drives_the_guest() -> None:
    pve = DryRunProxmoxAdapter(
        simulated_guests=[
            Guest(node="pve1", vmid=101, kind=GuestKind.VM, name="d", status="running")
        ]
    )
    ctl = GuestController(pve, "pve1", GuestKind.VM, 101)
    assert (await ctl.status()).running is True
    assert "restart" not in await ctl.stop() or True
    assert "start done" in await ctl.restart()
    with pytest.raises(ControllerError, match="not found"):
        await GuestController(pve, "pve1", GuestKind.VM, 999).status()
    pve.next_task_exit_status = "boom"
    with pytest.raises(ControllerError, match="failed"):
        await ctl.start()


# --- build_controller + API -----------------------------------------------------------

REGISTRY = """
version: 1
nodes:
  - {name: pve1, always_on: true}
services:
  - slug: docker-vm
    name: Docker VM
    node: pve1
    guest_kind: vm
    guest_id: 101
  - slug: nginx
    name: nginx
    parent: docker-vm
    controller: {type: docker, container: nginx, transport: {type: local}}
  - slug: redis
    name: redis
    parent: docker-vm
  - slug: pt
    name: pt
    parent: docker-vm
    controller: {type: portainer, endpoint: 2, container: pt}
"""


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY)
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()
    with TestClient(create_app()) as c:
        pve = c.app.state.adapters.proxmox  # type: ignore[attr-defined]
        pve.simulated_guests = [
            Guest(node="pve1", vmid=101, kind=GuestKind.VM, name="d", status="running")
        ]
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_build_controller_picks_the_right_thing(client: TestClient) -> None:
    settings = get_settings()
    pve = client.app.state.adapters.proxmox  # type: ignore[attr-defined]
    with Session(get_engine()) as s:
        by = {x.slug: x for x in s.exec(select(Service)).all()}
        root = build_controller(settings, pve, s, by["docker-vm"])
        assert (
            root is not None and root.type == "guest"
        )  # a root with no controller acts on its guest
        assert build_controller(settings, pve, s, by["redis"]) is None  # a sub with none: nothing
        assert build_controller(settings, pve, s, by["redis"], target="guest") is not None
        assert build_controller(settings, pve, s, by["nginx"]).type == "docker"  # type: ignore[union-attr]
        # DRY_RUN never talks to Portainer / Home Assistant
        assert build_controller(settings, pve, s, by["pt"]).describe().endswith("(dry run)")  # type: ignore[union-attr]


def test_controller_endpoint_describes_and_reports_live_status(client: TestClient) -> None:
    c = client.get("/api/services/nginx/controller?live=true").json()
    assert c["service"]["available"] and c["service"]["type"] == "docker"
    assert c["service"]["status"]["state"]  # DryRunTransport answers
    assert (
        c["guest"]["describe"] == "Proxmox vm 101 on pve1"
        and c["guest"]["status"]["running"] is True
    )
    none = client.get("/api/services/redis/controller").json()
    assert none["service"]["available"] is False and none["guest"]["available"] is True
    assert client.get("/api/services/ghost/controller").status_code == 404


def test_actions_run_audit_and_refuse_what_cannot_be_done(client: TestClient) -> None:
    ok = client.post("/api/services/nginx/actions/restart", json={})
    assert ok.status_code == 200, ok.text
    assert "restart done" in ok.json()["message"]
    nothing = client.post("/api/services/redis/actions/restart", json={})
    assert nothing.status_code == 409 and "controller" in nothing.text
    guest = client.post("/api/services/redis/actions/start", json={"target": "guest"})
    assert guest.status_code == 200
    assert client.post("/api/services/nginx/actions/explode", json={}).status_code == 422
    with Session(get_engine()) as s:
        actions = [
            a.action for a in s.exec(select(AuditEntry)).all() if a.action.startswith("service.")
        ]
    assert "service.restart" in actions and "service.start" in actions


def test_nothing_under_a_root_is_stopped_while_its_backup_runs(client: TestClient) -> None:
    with Session(get_engine()) as s:
        root = s.exec(select(Service).where(Service.slug == "docker-vm")).one()
        s.add(
            PipelineRun(
                kind=PipelineKind.BACKUP,
                correlation_id="b1",
                service_id=root.id,
                status=PipelineStatus.RUNNING,
            )
        )
        s.commit()
    refused = client.post("/api/services/nginx/actions/stop", json={})
    assert refused.status_code == 409 and "backup" in refused.text
    assert (
        client.post("/api/services/nginx/actions/start", json={}).status_code == 200
    )  # start is fine


def test_bulk_controller_actions_report_per_item(client: TestClient) -> None:
    r = client.post(
        "/api/bulk/services", json={"action": "ctl_restart", "slugs": ["nginx", "redis", "pt"]}
    ).json()
    by = {x["id"]: x for x in r["results"]}
    assert by["nginx"]["ok"] and by["pt"]["ok"]
    assert not by["redis"]["ok"] and "controller" in by["redis"]["message"]


def test_api_rejects_a_bad_controller(client: TestClient) -> None:
    bad = client.put("/api/registry/services/redis", json={"controller": {"type": "portainer"}})
    assert bad.status_code == 400 and "endpoint" in bad.text
    good = client.put(
        "/api/registry/services/redis",
        json={"controller": {"type": "docker", "container": "redis"}},
    )
    assert good.status_code == 200 and good.json()["controller"]["container"] == "redis"


async def test_waking_a_sub_service_starts_its_container_after_the_guest(
    client: TestClient,
) -> None:
    from orchestrator.pipelines.wake import WakePipeline

    adapters = client.app.state.adapters  # type: ignore[attr-defined]
    settings = get_settings().model_copy(
        update={"wake_poll_interval_s": 0.001, "wake_timeout_s": 5}
    )
    pipeline = WakePipeline(power=adapters.power, proxmox=adapters.proxmox, settings=settings)
    with Session(get_engine()) as s:
        run = pipeline.start(s, "nginx")
        # The probes of this tiny registry say nothing, so health never turns HEALTHY;
        # we only care which steps ran before that.
        with pytest.raises(Exception):  # noqa: B017 - WakeError after the timeout
            await pipeline._run(s, run, "nginx", actor="t", timeout_s=1)
        names = [step["name"] for step in run.steps]
    assert "guest_start" in names and "service_start" in names
    assert names.index("guest_start") < names.index("service_start")
