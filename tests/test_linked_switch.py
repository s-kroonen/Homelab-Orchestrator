"""What is linked to a service (proxy host, Portainer stack, guest), whether it is really on,
and the one switch that changes all of it."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.proxmox.base import Guest
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, PipelineRun, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind, PipelineKind, PipelineStatus
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
  - slug: dev-xymbols-db
    name: database
    parent: dev-xymbols
    controller: {type: portainer, endpoint: 1, container: dev-xymbols-db-1}
    # its own recovery keeps it a sub-service (a stack's other containers are folded into it)
    recovery: {enabled: true, max_attempts: 2}
  - slug: lonely
    name: nothing linked
    parent: portainer-vm
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
            Guest(node="pve1", vmid=101, kind=GuestKind.VM, name="p", status="running")
        ]
        _make_power_stateful(pve, monkeypatch)
        _login_as_first_admin(c, monkeypatch)
        yield c


def _make_power_stateful(pve: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dry-run Proxmox only records start / stop; let them move the simulated guest."""
    start, stop = pve.start_guest, pve.stop_guest

    def mover(inner: Any, status: str) -> Any:
        async def move(node: str, vmid: int, kind: GuestKind) -> Any:
            handle = await inner(node, vmid, kind)
            pve.simulated_guests = [
                replace(g, status=status) if g.vmid == vmid else g for g in pve.simulated_guests
            ]
            return handle

        return move

    monkeypatch.setattr(pve, "start_guest", mover(start, "running"))
    monkeypatch.setattr(pve, "stop_guest", mover(stop, "stopped"))


def _portainer(client: TestClient) -> Any:
    return client.app.state.adapters.portainer  # type: ignore[attr-defined]


def _host(client: TestClient, slug: str, hostname: str = "dev.xymbols.example") -> dict[str, Any]:
    with Session(get_engine()) as s:
        sid = s.exec(select(Service).where(Service.slug == slug)).one().id
    r = client.post(
        "/api/proxy-hosts",
        json={"hostname": hostname, "upstream": "http://10.0.0.9:8081", "service_id": sid},
    )
    assert r.status_code == 201, r.text
    assert client.post("/api/push", json={}).status_code == 200  # the route now exists
    out: dict[str, Any] = r.json()
    return out


def _state(client: TestClient, slug: str) -> dict[str, Any]:
    r = client.get(f"/api/services/{slug}/linked")
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def _switch(client: TestClient, slug: str, state: str, **body: Any) -> Any:
    return client.post(f"/api/services/{slug}/switch", json={"state": state, **body})


# -- reading -------------------------------------------------------------------------------------


def test_state_reads_proxy_stack_and_says_they_agree(client: TestClient) -> None:
    _host(client, "dev-xymbols")
    st = _state(client, "dev-xymbols")
    assert st["state"] == "on" and st["drift"] == ""
    kinds = {p["kind"]: p for p in st["parts"]}
    assert kinds["stack"]["on"] is True and "2/2 containers" in kinds["stack"]["detail"]
    assert kinds["stack"]["open_url"].startswith("https://portainer.example.lan/")
    assert kinds["proxy"]["on"] is True and kinds["proxy"]["detail"] == "served"
    assert "guest" not in kinds  # a sub-service does not own the guest


def test_root_includes_its_guest(client: TestClient) -> None:
    st = _state(client, "portainer-vm")
    assert [p["kind"] for p in st["parts"]] == ["guest"]
    assert st["parts"][0]["on"] is True and st["state"] == "on"


def test_nothing_linked_is_none_not_off(client: TestClient) -> None:
    assert _state(client, "lonely")["state"] == "none"
    assert client.get("/api/services/ghost/linked").status_code == 404


def test_a_change_made_in_portainer_shows_as_mixed_never_fixed(client: TestClient) -> None:
    _host(client, "dev-xymbols")
    p = _portainer(client)

    async def stop() -> None:
        stack = await p.get_stack(1, "dev-xymbols")
        await p.stop_stack(stack)

    import asyncio

    asyncio.run(stop())
    st = _state(client, "dev-xymbols")
    assert st["state"] == "mixed"
    assert st["disagreeing"] == ["dev-xymbols"]
    assert "stack dev-xymbols off" in st["drift"]
    assert "linked parts disagree" in st["drift"]
    # reading is read-only: nothing was started again
    assert p.calls == ["stop dev-xymbols"]


def test_a_proxy_host_with_its_own_switch_off_does_not_make_it_mixed(client: TestClient) -> None:
    host = _host(client, "dev-xymbols")
    assert (
        client.post(f"/api/proxy-hosts/{host['id']}/enabled", json={"enabled": False}).status_code
        == 200
    )
    st = _state(client, "dev-xymbols")
    proxy = next(p for p in st["parts"] if p["kind"] == "proxy")
    assert proxy["deliberate"] is True
    assert st["state"] == "on"


def test_linked_all_lists_only_services_with_something_linked(client: TestClient) -> None:
    data = client.get("/api/services/linked/all").json()
    assert set(data) == {"portainer-vm", "dev-xymbols", "dev-xymbols-db"}


# -- switching ----------------------------------------------------------------------------------


def test_switching_off_unserves_the_proxy_then_stops_the_stack(client: TestClient) -> None:
    _host(client, "dev-xymbols")
    r = _switch(client, "dev-xymbols", "off")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["state"]["state"] == "off"
    names = [s["name"] for s in body["steps"]]
    assert names.index("proxy unserve") < names.index("stop stack dev-xymbols")
    # the database container below it goes first
    assert _portainer(client).calls == ["stop container dev-xymbols-db-1", "stop dev-xymbols"]
    with Session(get_engine()) as s:
        assert s.exec(select(Service).where(Service.slug == "dev-xymbols")).one().enabled is False
    # the run is in the feed with its own audit entries
    with Session(get_engine()) as s:
        run = s.exec(select(PipelineRun).where(PipelineRun.kind == PipelineKind.SWITCH)).one()
        assert run.status is PipelineStatus.SUCCEEDED
        actions = [a.action for a in s.exec(select(AuditEntry)).all()]
    assert "service.switch_off" in actions and "service.switch_off.done" in actions


def test_switching_a_parent_off_takes_the_children_down_first(client: TestClient) -> None:
    _host(client, "dev-xymbols")
    _host(client, "dev-xymbols-db", "db.xymbols.example")
    r = _switch(client, "dev-xymbols", "off")
    assert r.status_code == 200, r.text
    stops = [s["name"] for s in r.json()["steps"] if s["name"].startswith("stop ")]
    assert stops == ["stop container dev-xymbols-db-1", "stop stack dev-xymbols"]
    # the child's own intent is untouched: it comes back with the parent
    with Session(get_engine()) as s:
        child = s.exec(select(Service).where(Service.slug == "dev-xymbols-db")).one()
        assert child.enabled is True
    assert _state(client, "dev-xymbols-db")["served"] is False


def test_switching_on_starts_the_stack_waits_then_serves(client: TestClient) -> None:
    _host(client, "dev-xymbols")
    assert _switch(client, "dev-xymbols", "off").status_code == 200
    p = _portainer(client)
    p.calls.clear()
    r = _switch(client, "dev-xymbols", "on", restore_children=False)
    assert r.status_code == 200, r.text
    body = r.json()
    names = [s["name"] for s in body["steps"]]
    assert (
        names.index("start stack dev-xymbols") < names.index("intent") < names.index("proxy serve")
    )
    assert p.calls == ["start dev-xymbols"]
    assert body["state"]["state"] == "on"


def test_children_are_only_watched_unless_asked_to_come_back(client: TestClient) -> None:
    _host(client, "dev-xymbols")
    _switch(client, "dev-xymbols", "off")
    p = _portainer(client)
    p.calls.clear()
    # The stack comes back; its database container stays as the stack start left it (the stack
    # start brings containers up in Portainer), so nothing extra is started here.
    _switch(client, "dev-xymbols", "on")
    assert all("container" not in c for c in p.calls)


def test_restore_children_starts_what_was_on(client: TestClient) -> None:
    import asyncio

    p = _portainer(client)
    assert _switch(client, "dev-xymbols", "off").status_code == 200
    asyncio.run(p.container_action(1, "dev-xymbols-db-1", "stop"))
    p.calls.clear()
    assert _switch(client, "dev-xymbols", "on", restore_children=True).status_code == 200
    assert "start dev-xymbols" in p.calls


def test_the_guest_is_only_stopped_when_asked(client: TestClient) -> None:
    r = _switch(client, "portainer-vm", "off")
    assert r.status_code == 200
    step = next(s for s in r.json()["steps"] if "vm 101" in s["name"])
    assert step["status"] == "skipped" and "not confirmed" in step["detail"]["reason"]
    assert r.json()["state"]["state"] == "on"  # the guest is still up, and it says so
    again = _switch(client, "portainer-vm", "off", include_guest=True)
    assert again.status_code == 200 and again.json()["state"]["state"] == "off"
    back = _switch(client, "portainer-vm", "on")
    assert back.status_code == 200 and back.json()["state"]["state"] == "on"


def test_a_sub_service_cannot_come_on_under_a_switched_off_parent(client: TestClient) -> None:
    assert _switch(client, "portainer-vm", "off").status_code == 200
    r = _switch(client, "dev-xymbols", "on")
    assert r.status_code == 409 and "portainer-vm" in r.text


def test_nothing_is_switched_off_while_a_backup_runs(client: TestClient) -> None:
    with Session(get_engine()) as s:
        root = s.exec(select(Service).where(Service.slug == "portainer-vm")).one()
        s.add(
            PipelineRun(
                kind=PipelineKind.BACKUP,
                correlation_id="b1",
                service_id=root.id,
                status=PipelineStatus.RUNNING,
            )
        )
        s.commit()
    r = _switch(client, "dev-xymbols", "off")
    assert r.status_code == 409 and "backup" in r.text
    assert _portainer(client).calls == []


def test_a_second_switch_while_one_runs_is_refused(client: TestClient) -> None:
    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.slug == "dev-xymbols")).one()
        s.add(
            PipelineRun(
                kind=PipelineKind.SWITCH,
                correlation_id="s1",
                service_id=svc.id,
                status=PipelineStatus.RUNNING,
            )
        )
        s.commit()
    assert _switch(client, "dev-xymbols", "off").status_code == 409


def test_a_failed_stop_is_reported_and_the_rest_still_runs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(client, "dev-xymbols")
    p = _portainer(client)

    async def refuse(stack: Any) -> None:
        raise __import__("orchestrator.adapters.errors", fromlist=["x"]).AdapterRequestError("nope")

    monkeypatch.setattr(p, "stop_stack", refuse)
    r = _switch(client, "dev-xymbols", "off")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "nope" in body["message"]
    assert body["state"]["state"] == "mixed"  # proxy unserved, stack still up
    with Session(get_engine()) as s:
        run = s.exec(select(PipelineRun).where(PipelineRun.kind == PipelineKind.SWITCH)).one()
        assert run.status is PipelineStatus.FAILED


def test_read_only_portainer_refuses_the_stop_but_the_proxy_is_still_unserved(
    client: TestClient,
) -> None:
    _host(client, "dev-xymbols")
    _portainer(client)._read_only = True
    body = _switch(client, "dev-xymbols", "off").json()
    assert body["ok"] is False and "read-only" in body["message"]
    proxy = next(p for p in body["state"]["parts"] if p["kind"] == "proxy")
    assert proxy["on"] is False


def test_unknown_service_and_bad_body(client: TestClient) -> None:
    assert _switch(client, "ghost", "off").status_code == 404
    assert (
        client.post("/api/services/dev-xymbols/switch", json={"state": "sideways"}).status_code
        == 422
    )


# -- the outside-change watcher ---------------------------------------------------------------------


def test_an_outside_change_is_reported_once_and_cleared_when_it_agrees_again(
    client: TestClient,
) -> None:
    import asyncio

    from orchestrator.config import get_settings
    from orchestrator.registry.linked_watch import LinkedWatcher

    _host(client, "dev-xymbols")
    adapters = client.app.state.adapters  # type: ignore[attr-defined]
    p = adapters.portainer
    watcher = LinkedWatcher()

    async def observe() -> int:
        with Session(get_engine()) as s:
            return await watcher.observe(s, adapters, get_settings())

    assert asyncio.run(observe()) == 0  # everything agrees

    async def stop() -> None:
        await p.stop_stack(await p.get_stack(1, "dev-xymbols"))

    asyncio.run(stop())
    assert asyncio.run(observe()) == 0  # seen once: could be a blip or a switch in progress
    # still the same: report it - the stack (its proxy is still served: mixed) and the database
    # container below it (stopped along with the stack)
    assert asyncio.run(observe()) == 2
    assert asyncio.run(observe()) == 0  # ...but only once

    with Session(get_engine()) as s:
        rows = [a for a in s.exec(select(AuditEntry)).all() if a.action == "service.outside_change"]
    by_target = {r.target: r for r in rows}
    assert set(by_target) == {"service:dev-xymbols", "service:dev-xymbols-db"}
    assert by_target["service:dev-xymbols"].details["state"] == "mixed"
    assert by_target["service:dev-xymbols-db"].details["state"] == "off"

    async def start() -> None:
        await p.start_stack(await p.get_stack(1, "dev-xymbols"))

    asyncio.run(start())
    assert asyncio.run(observe()) == 0
    with Session(get_engine()) as s:
        cleared = [a for a in s.exec(select(AuditEntry)).all() if a.action.endswith("_cleared")]
    assert len(cleared) == 2
    assert p.calls == ["stop dev-xymbols", "start dev-xymbols"]  # only what the test did


# -- CLI ------------------------------------------------------------------------------------------


def test_cli_state_disable_enable_and_the_registry_file_follows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml
    from click.testing import CliRunner

    from orchestrator.cli import cli

    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY)
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()

    run = CliRunner()
    state = run.invoke(cli, ["service", "state", "dev-xymbols"])
    assert state.exit_code == 0, state.output
    assert "ON" in state.output and "stack dev-xymbols" in state.output

    off = run.invoke(cli, ["service", "disable", "dev-xymbols"])
    assert off.exit_code == 0, off.output
    assert "stop stack dev-xymbols" in off.output and "OFF" in off.output
    saved = yaml.safe_load(path.read_text())
    assert next(x for x in saved["services"] if x["slug"] == "dev-xymbols")["enabled"] is False

    on = run.invoke(cli, ["service", "enable", "dev-xymbols"])
    assert on.exit_code == 0, on.output
    assert "start stack dev-xymbols" in on.output
    saved = yaml.safe_load(path.read_text())
    assert next(x for x in saved["services"] if x["slug"] == "dev-xymbols")["enabled"] is True

    refused = run.invoke(cli, ["service", "enable", "ghost"])
    assert refused.exit_code != 0 and "no service" in refused.output


# -- a slow or unreachable Portainer must not slow everything down ------------------------------------


def test_an_unreachable_portainer_is_asked_once_per_pass(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from orchestrator.adapters.errors import AdapterUnreachable
    from orchestrator.config import get_settings
    from orchestrator.registry.linked import gather_context, read_linked

    adapters = client.app.state.adapters  # type: ignore[attr-defined]
    asked: list[str] = []

    async def down(endpoint_id: int, name: str) -> Any:
        asked.append(name)
        raise AdapterUnreachable("Portainer unreachable")

    monkeypatch.setattr(adapters.portainer, "get_stack", down)

    async def go() -> list[Any]:
        with Session(get_engine()) as s:
            ctx = await gather_context(s, adapters, light=True)
            out = []
            for slug in ("dev-xymbols", "dev-xymbols-db"):
                svc = s.exec(select(Service).where(Service.slug == slug)).one()
                out.append(await read_linked(s, adapters, get_settings(), svc, ctx))
            return out

    states = asyncio.run(go())
    assert asked == ["dev-xymbols"]  # the container service did not ask again
    assert all(p.on is None for st in states for p in st.parts)


def test_the_light_pass_does_not_run_command_controllers(client: TestClient) -> None:
    import asyncio

    from orchestrator.config import get_settings
    from orchestrator.registry.linked import gather_context, read_linked

    adapters = client.app.state.adapters  # type: ignore[attr-defined]

    async def go() -> tuple[int, int]:
        with Session(get_engine()) as s:
            svc = s.exec(select(Service).where(Service.slug == "lonely")).one()
            svc.controller = {"type": "docker", "container": "x", "transport": {"type": "local"}}
            s.add(svc)
            s.commit()
            light = await read_linked(
                s, adapters, get_settings(), svc, await gather_context(s, adapters, light=True)
            )
            full = await read_linked(
                s, adapters, get_settings(), svc, await gather_context(s, adapters)
            )
            return len(light.parts), len(full.parts)

    assert asyncio.run(go()) == (0, 1)


# -- a node or guest that is off ------------------------------------------------------------------


def _node_off(client: TestClient) -> None:
    from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus

    pve = client.app.state.adapters.proxmox  # type: ignore[attr-defined]
    pve.simulated_cluster = ClusterStatus(nodes=[ClusterNode(name="pve1", online=False)])
    pve.simulated_guests = []  # an offline node lists no guests


def test_a_node_that_is_off_makes_everything_on_it_off_not_unknown(client: TestClient) -> None:
    _host(client, "dev-xymbols")
    _node_off(client)
    portainer = _portainer(client)
    asked: list[Any] = []
    real = portainer.get_stack

    async def spy(*a: Any, **k: Any) -> Any:
        asked.append(a)
        return await real(*a, **k)

    portainer.get_stack = spy
    st = _state(client, "dev-xymbols")
    parts = {p["kind"]: p for p in st["parts"]}
    assert parts["stack"]["on"] is False and "offline" in parts["stack"]["detail"]
    assert "nothing is running behind it" in parts["proxy"]["detail"]
    assert st["state"] == "off" and st["host_down"]
    assert st["drift"].startswith("asked to be on, but node 'pve1' is offline")
    assert st["disagreeing"] == [] and st["unknown"] == []
    assert asked == []  # Portainer was not even asked

    root = _state(client, "portainer-vm")
    assert root["parts"][0]["on"] is False and "offline" in root["parts"][0]["detail"]
    assert root["state"] == "off"


def test_a_stopped_guest_is_the_same_story(client: TestClient) -> None:
    _host(client, "dev-xymbols")
    pve = client.app.state.adapters.proxmox  # type: ignore[attr-defined]
    pve.simulated_guests = [replace(g, status="stopped") for g in pve.simulated_guests]
    st = _state(client, "dev-xymbols")
    assert st["state"] == "off" and "stopped" in st["host_down"]
    assert next(p for p in st["parts"] if p["kind"] == "stack")["on"] is False


def test_an_unreadable_proxmox_is_still_unknown_not_off(client: TestClient) -> None:
    _host(client, "dev-xymbols")
    pve = client.app.state.adapters.proxmox  # type: ignore[attr-defined]

    async def boom() -> Any:
        from orchestrator.adapters.errors import AdapterUnreachable

        raise AdapterUnreachable("no route to proxmox")

    pve.cluster_status = boom
    st = _state(client, "dev-xymbols")
    assert st["host_down"] == ""
    assert next(p for p in st["parts"] if p["kind"] == "stack")["on"] is True  # Portainer answered


def test_switching_on_under_an_offline_node_says_why_instead_of_failing_blindly(
    client: TestClient,
) -> None:
    _host(client, "dev-xymbols")
    assert _switch(client, "dev-xymbols", "off").status_code == 200
    _node_off(client)
    out = _switch(client, "dev-xymbols", "on").json()
    assert out["ok"] is False
    failed = [s for s in out["steps"] if s["status"] == "failed"]
    assert (
        failed
        and "offline" in failed[0]["detail"]["error"]
        and "wake" in failed[0]["detail"]["error"]
    )


def test_a_node_that_is_off_is_not_reported_as_changed_outside(client: TestClient) -> None:
    import asyncio

    from orchestrator.config import get_settings
    from orchestrator.registry.linked_watch import CONFIRM_CYCLES, LinkedWatcher

    _host(client, "dev-xymbols")
    _node_off(client)
    adapters = client.app.state.adapters  # type: ignore[attr-defined]
    watcher = LinkedWatcher()
    with Session(get_engine()) as s:
        for _ in range(CONFIRM_CYCLES + 1):
            reported = asyncio.run(watcher.observe(s, adapters, get_settings()))
        assert reported == 0


def test_a_restart_does_not_announce_the_same_standing_change_again(client: TestClient) -> None:
    import asyncio

    from orchestrator.config import get_settings
    from orchestrator.registry.linked_watch import CONFIRM_CYCLES, LinkedWatcher

    _host(client, "dev-xymbols")
    adapters = client.app.state.adapters  # type: ignore[attr-defined]
    p = adapters.portainer
    asyncio.run(_stop_stack(p))

    async def observe(watcher: LinkedWatcher) -> int:
        with Session(get_engine()) as s:
            return await watcher.observe(s, adapters, get_settings())

    first = LinkedWatcher()
    reported = sum(asyncio.run(observe(first)) for _ in range(CONFIRM_CYCLES + 1))
    assert reported == 2  # the stack and the container below it

    restarted = LinkedWatcher()  # a new process: nothing in memory
    assert sum(asyncio.run(observe(restarted)) for _ in range(CONFIRM_CYCLES + 2)) == 0

    asyncio.run(_start_stack(p))  # it agrees again: cleared once, and a new change is news again
    assert asyncio.run(observe(restarted)) == 0
    asyncio.run(_stop_stack(p))
    assert sum(asyncio.run(observe(restarted)) for _ in range(CONFIRM_CYCLES + 1)) == 2


async def _stop_stack(p: Any) -> None:
    await p.stop_stack(await p.get_stack(1, "dev-xymbols"))


async def _start_stack(p: Any) -> None:
    await p.start_stack(await p.get_stack(1, "dev-xymbols"))


# -- a host that was never pushed is waiting, not changed outside ----------------------------------


def _unpushed_host(client: TestClient, hostname: str, slug: str = "dev-xymbols") -> int:
    with Session(get_engine()) as s:
        sid = s.exec(select(Service).where(Service.slug == slug)).one().id
    r = client.post(
        "/api/proxy-hosts",
        json={"hostname": hostname, "upstream": "http://10.0.0.9:8081", "service_id": sid},
    )
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


def test_a_host_that_was_never_pushed_is_pending_and_not_an_outside_change(
    client: TestClient,
) -> None:
    _unpushed_host(client, "new.example.com")
    st = _state(client, "dev-xymbols")
    part = next(p for p in st["parts"] if p["kind"] == "proxy")
    assert part["pending"] is True and "not pushed" in part["detail"]
    assert st["disagreeing"] == [] and st["drift"] == ""  # it was not changed by anyone
    assert st["state"] == "on"  # judged by what exists (the stack)


def test_pushing_it_makes_it_a_normal_part_and_a_removed_route_is_a_real_change(
    client: TestClient,
) -> None:
    _unpushed_host(client, "new.example.com")
    assert client.post("/api/push", json={}).status_code == 200
    part = next(p for p in _state(client, "dev-xymbols")["parts"] if p["kind"] == "proxy")
    assert part["pending"] is False and part["on"] is True
    # someone removes the route in Traefik Manager afterwards
    proxy = client.app.state.adapters.proxy  # type: ignore[attr-defined]
    asyncio_run(proxy.delete_route(part_ref(client)))
    st = _state(client, "dev-xymbols")
    gone = next(p for p in st["parts"] if p["kind"] == "proxy")
    assert gone["on"] is False and gone["pending"] is False and "gone" in gone["detail"]
    assert st["state"] == "mixed"


def test_a_disabled_host_stays_disabled_through_a_push(client: TestClient) -> None:
    host_id = _unpushed_host(client, "off.example.com")
    assert (
        client.post(f"/api/proxy-hosts/{host_id}/enabled", json={"enabled": False}).status_code
        == 200
    )
    part = next(p for p in _state(client, "dev-xymbols")["parts"] if p["kind"] == "proxy")
    assert part["deliberate"] is True and part["pending"] is False  # its own switch, not pending
    assert client.post("/api/push", json={}).status_code == 200
    shown = client.get(f"/api/proxy-hosts/{host_id}").json()
    assert shown["enabled"] is False
    part = next(p for p in _state(client, "dev-xymbols")["parts"] if p["kind"] == "proxy")
    assert part["on"] is False and part["deliberate"] is True  # parked, still its own switch


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def part_ref(client: TestClient) -> str:
    with Session(get_engine()) as s:
        from orchestrator.db.models import ProxyHost
        from orchestrator.registry.proxy_push import ref_of

        return str(ref_of(s.exec(select(ProxyHost)).one()))
