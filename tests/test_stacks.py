"""Adopting, creating, deleting and controlling Portainer stacks; linking proxy hosts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.cli import cli
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, Probe, ProxyHost, Service, Setting
from orchestrator.db.session import get_engine
from orchestrator.registry import adopt as adopt_module
from orchestrator.registry.loader import reconcile_yaml_into_db
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin

REGISTRY = """\
version: 1
nodes:
  - {name: pve1, always_on: true}
services:
  - slug: portainer-vm
    name: Portainer VM
    node: pve1
    guest_kind: vm
    guest_id: 101
"""


def setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY)
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    monkeypatch.setenv("WAKE_POLL_INTERVAL_S", "0.001")
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()


def add_host(hostname: str, upstream: str, owner: str = "portainer-vm") -> None:
    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.slug == owner)).one()
        s.add(ProxyHost(hostname=hostname, upstream=upstream, service_id=svc.id))
        s.commit()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    setup_env(tmp_path, monkeypatch)
    with TestClient(create_app()) as c:
        # after start-up, which reconciles the registry file (it has no proxy hosts)
        add_host("dev.xymbols.example", "http://10.0.0.9:8081")
        add_host("other.example", "http://10.0.0.9:9999")
        _login_as_first_admin(c, monkeypatch)
        assert c.post("/api/push", json={}).status_code == 200  # the routes exist in Traefik
        yield c


def portainer(client: TestClient) -> Any:
    return client.app.state.adapters.portainer  # type: ignore[attr-defined]


def slugs() -> set[str]:
    with Session(get_engine()) as s:
        return {x.slug for x in s.exec(select(Service)).all()}


ADOPT = {
    "endpoint_id": 1,
    "stack": "dev-xymbols",
    "slug": "dev-xymbols",
    "parent": "portainer-vm",
    "children": [
        {"container": "dev-xymbols-web-1", "slug": "dev-xymbols-web"},
        {"container": "dev-xymbols-db-1", "slug": "dev-xymbols-db", "backup_gate": "required"},
    ],
    "hosts": ["dev.xymbols.example"],
}


# -- suggest ---------------------------------------------------------------------------------------


def test_suggest_proposes_the_whole_link_up(client: TestClient) -> None:
    r = client.get("/api/stacks/suggest", params={"endpoint": 1, "name": "dev-xymbols"})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["parent"] == "portainer-vm" and s["slug"] == "dev-xymbols" and s["kind"] == "build"
    by = {c["container"]: c for c in s["children"]}
    assert (
        by["dev-xymbols-db-1"]["likely_db"] and by["dev-xymbols-db-1"]["backup_gate"] == "required"
    )
    assert not by["dev-xymbols-web-1"]["likely_db"]
    assert [h["hostname"] for h in s["hosts"]] == ["dev.xymbols.example"]  # port 8081 only
    assert s["hosts"][0]["linked_to"] == "portainer-vm"
    kinds = [(p["service"], p["kind"]) for p in s["probes"]]
    assert ("stack", "portainer") in kinds and ("stack", "http") in kinds
    assert s["has_git"] is True and "probes+backup" in s["deploy_presets"]


def test_suggest_errors(client: TestClient) -> None:
    assert (
        client.get("/api/stacks/suggest", params={"endpoint": 1, "name": "ghost"}).status_code
        == 404
    )


def test_an_unmanaged_stack_is_flagged(client: TestClient) -> None:
    s = client.get("/api/stacks/suggest", params={"endpoint": 1, "name": "legacy-tools"}).json()
    assert any("did not create" in n for n in s["notes"]) and s["has_git"] is False


# -- adopt -------------------------------------------------------------------------------------------


def test_adopt_creates_the_services_links_the_host_and_adds_probes(client: TestClient) -> None:
    r = client.post(
        "/api/stacks/adopt", json={**ADOPT, "deploy": "probes+backup", "webhook": "gitlab"}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["services"] == ["dev-xymbols", "dev-xymbols-web", "dev-xymbols-db"]
    assert body["hosts"] == ["dev.xymbols.example"] and body["probes"] == 4
    assert body["webhook_secret"] and len(body["webhook_secret"]) > 20

    with Session(get_engine()) as s:
        by = {x.slug: x for x in s.exec(select(Service)).all()}
        stack = by["dev-xymbols"]
        assert stack.controller == {"type": "portainer", "endpoint": 1, "stack": "dev-xymbols"}
        assert by["dev-xymbols-web"].parent_id == stack.id
        assert by["dev-xymbols-db"].backup_gate.value == "required"
        assert stack.deploy["guard"]["pre"]["backup"]["max_age_h"] == 24
        assert stack.deploy["trigger"]["webhook"]["provider"] == "gitlab"
        host = s.exec(select(ProxyHost).where(ProxyHost.hostname == "dev.xymbols.example")).one()
        assert host.service_id == stack.id
        probes = {(x.service_id, x.name, x.kind.value) for x in s.exec(select(Probe)).all()}
        assert (stack.id, "portainer", "portainer") in probes and (
            stack.id,
            "http",
            "http",
        ) in probes
        db_probe = next(p for p in s.exec(select(Probe)) if p.service_id == by["dev-xymbols-db"].id)
        assert db_probe.required is True  # the database matters
        web_probe = next(
            p for p in s.exec(select(Probe)) if p.service_id == by["dev-xymbols-web"].id
        )
        assert web_probe.required is False
        assert s.get(Setting, "deploy.webhook.dev-xymbols") is not None
        actions = [a.action for a in s.exec(select(AuditEntry)).all()]
    assert "stack.adopted" in actions and "service.created" in actions
    state = client.get("/api/services/dev-xymbols/linked").json()
    assert state["state"] == "on"
    assert body["webhook_secret"] not in json.dumps(state)


def test_adopting_twice_is_refused(client: TestClient) -> None:
    assert client.post("/api/stacks/adopt", json=ADOPT).status_code == 201
    again = client.post("/api/stacks/adopt", json={**ADOPT, "slug": "again"})
    assert again.status_code == 409 and "already linked" in again.text


@pytest.mark.parametrize(
    ("change", "status", "message"),
    [
        ({"parent": "ghost"}, 400, "choose the service"),
        ({"stack": "ghost"}, 404, "no stack"),
        ({"children": [{"container": "nope", "slug": "x"}]}, 400, "no container"),
        ({"hosts": ["ghost.example"]}, 404, "no proxy host"),
        ({"slug": "Bad Slug"}, 400, "invalid slug"),
    ],
)
def test_adopt_checks_everything_before_changing_anything(
    client: TestClient, change: dict[str, Any], status: int, message: str
) -> None:
    before = slugs()
    r = client.post("/api/stacks/adopt", json={**ADOPT, **change})
    assert r.status_code == status and message in r.text, r.text
    assert slugs() == before


def test_a_failure_part_way_undoes_what_was_made(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_: Any, **__: Any) -> int:
        raise adopt_module.StackError("probe creation failed", 500)

    monkeypatch.setattr(adopt_module, "_add_probes", boom)
    before = slugs()
    r = client.post("/api/stacks/adopt", json=ADOPT)
    assert r.status_code == 500
    assert slugs() == before
    with Session(get_engine()) as s:
        host = s.exec(select(ProxyHost).where(ProxyHost.hostname == "dev.xymbols.example")).one()
        owner = s.exec(select(Service).where(Service.slug == "portainer-vm")).one()
        assert host.service_id == owner.id  # back where it was


def test_a_stack_without_git_gets_no_deploy_method(client: TestClient) -> None:
    plan = {"endpoint_id": 1, "stack": "media", "slug": "media", "parent": "portainer-vm"}
    refused = client.post("/api/stacks/adopt", json={**plan, "deploy": "probes"})
    assert refused.status_code == 409 and "Git" in refused.text
    assert client.post("/api/stacks/adopt", json=plan).status_code == 201


# -- create ------------------------------------------------------------------------------------------


CREATE = {
    "endpoint_id": 1,
    "name": "newapp",
    "repository_url": "https://git.example/app.git",
    "reference": "refs/heads/dev",
    "git_password": "hunter2",
    "env": {"DB_PASSWORD": "s3cret-value"},
}


def test_preview_masks_every_secret(client: TestClient) -> None:
    r = client.post("/api/stacks/preview", json=CREATE)
    assert r.status_code == 200, r.text
    assert "s3cret-value" not in r.text and "hunter2" not in r.text
    assert r.json()["body"]["env"] == [{"name": "DB_PASSWORD", "value": "***"}]


def test_create_needs_the_confirmation(client: TestClient) -> None:
    r = client.post("/api/stacks", json={"spec": CREATE})
    assert r.status_code == 400 and "preview" in r.text
    assert "newapp" not in [
        c.split(" ", 1)[1] for c in portainer(client).calls if c.startswith("create")
    ]


def test_create_goes_through_portainer_and_keeps_no_secret(client: TestClient) -> None:
    r = client.post(
        "/api/stacks",
        json={"spec": CREATE, "confirm": True, "adopt": {**ADOPT, "stack": "x", "endpoint_id": 1,
              "slug": "newapp", "children": [], "hosts": []}},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["stack"]["name"] == "newapp" and body["adopted"]["services"] == ["newapp"]
    assert portainer(client).calls[-1] == "create newapp"
    assert "s3cret-value" not in r.text and "hunter2" not in r.text
    with Session(get_engine()) as s:
        for a in s.exec(select(AuditEntry)).all():
            assert "s3cret-value" not in json.dumps(a.details) and "hunter2" not in json.dumps(
                a.details
            )
        created = [a for a in s.exec(select(AuditEntry)).all() if a.action == "stack.create"]
    assert len(created) == 1
    with Session(get_engine()) as s:
        row = s.exec(select(Service).where(Service.slug == "newapp")).one()
        assert row.controller["stack"] == "newapp"


def test_creating_a_duplicate_is_refused(client: TestClient) -> None:
    r = client.post("/api/stacks", json={"spec": {**CREATE, "name": "media"}, "confirm": True})
    assert r.status_code == 409


def test_create_from_text_needs_the_text(client: TestClient) -> None:
    r = client.post("/api/stacks/preview", json={"endpoint_id": 1, "name": "t", "source": "string"})
    assert r.status_code == 400 and "compose text" in r.text


def test_read_only_portainer_creates_nothing(client: TestClient) -> None:
    portainer(client)._read_only = True
    r = client.post("/api/stacks", json={"spec": CREATE, "confirm": True})
    assert r.status_code == 502 and "read-only" in r.text


# -- delete ------------------------------------------------------------------------------------------


def adopt_dev(client: TestClient) -> None:
    assert client.post("/api/stacks/adopt", json=ADOPT).status_code == 201


def delete(client: TestClient, name: str, **body: Any) -> Any:
    return client.request("DELETE", f"/api/stacks/1/{name}", json=body)


def test_delete_asks_for_the_name(client: TestClient) -> None:
    r = delete(client, "media", confirm_name="medi")
    assert r.status_code == 400 and "type the stack's name" in r.text
    assert "delete media" not in portainer(client).calls


def test_delete_refuses_while_something_linked_is_on(client: TestClient) -> None:
    adopt_dev(client)
    r = delete(client, "dev-xymbols", confirm_name="dev-xymbols")
    assert r.status_code == 409 and "switch" in r.text
    assert "delete dev-xymbols" not in portainer(client).calls
    assert "dev-xymbols" in slugs()


def test_delete_can_take_it_down_first_and_removes_the_link(client: TestClient) -> None:
    adopt_dev(client)
    r = delete(client, "dev-xymbols", confirm_name="dev-xymbols", take_down=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "dev-xymbols" in body["taken_down"]
    assert set(body["removed_services"]) == {"dev-xymbols", "dev-xymbols-web", "dev-xymbols-db"}
    assert body["unlinked_hosts"] == ["dev.xymbols.example"]
    calls = portainer(client).calls
    assert calls.index("stop dev-xymbols") < calls.index("delete dev-xymbols")  # down, then gone
    assert slugs() == {"portainer-vm"}
    with Session(get_engine()) as s:
        host = s.exec(select(ProxyHost).where(ProxyHost.hostname == "dev.xymbols.example")).one()
        assert host.service_id is None  # the host stays; only the link went
    assert (
        client.get("/api/stacks/suggest", params={"endpoint": 1, "name": "dev-xymbols"}).status_code
        == 404
    )


def test_delete_can_keep_the_services(client: TestClient) -> None:
    adopt_dev(client)
    assert client.post("/api/services/dev-xymbols/switch", json={"state": "off"}).status_code == 200
    r = delete(client, "dev-xymbols", confirm_name="dev-xymbols", remove_services=False)
    assert r.status_code == 200 and r.json()["removed_services"] == []
    with Session(get_engine()) as s:
        row = s.exec(select(Service).where(Service.slug == "dev-xymbols")).one()
        assert row.controller == {} and row.deploy == {}


def test_delete_of_an_unlinked_stack_just_deletes_it(client: TestClient) -> None:
    r = delete(client, "media", confirm_name="media")
    assert r.status_code == 200 and portainer(client).calls == ["delete media"]
    assert delete(client, "ghost", confirm_name="ghost").status_code == 404


# -- control ----------------------------------------------------------------------------------------


def test_stack_and_container_control_are_audited(client: TestClient) -> None:
    assert client.post("/api/stacks/1/media/stop").status_code == 200
    assert client.post("/api/stacks/1/media/start").status_code == 200
    assert client.post("/api/stacks/1/media/restart").status_code == 200
    assert client.post("/api/containers/1/media-sonarr-1/start").status_code == 200
    assert portainer(client).calls == [
        "stop media", "start media", "stop media", "start media", "start container media-sonarr-1",
    ]  # fmt: skip
    assert client.post("/api/containers/1/ghost/start").status_code == 502
    assert client.post("/api/stacks/1/ghost/stop").status_code == 404
    assert client.post("/api/stacks/1/media/explode").status_code == 422
    with Session(get_engine()) as s:
        actions = {a.action for a in s.exec(select(AuditEntry)).all()}
    assert {"stack.stop", "stack.start", "stack.restart", "container.start"} <= actions


# -- proxy hosts from the service side ---------------------------------------------------------------


def test_candidates_link_unlink_and_move(client: TestClient) -> None:
    adopt_dev(client)
    c = client.get("/api/services/dev-xymbols/proxy-host-candidates").json()
    assert [h["hostname"] for h in c["candidates"]] == ["dev.xymbols.example"]
    assert c["linked"] == ["dev.xymbols.example"]

    assert (
        client.delete("/api/services/dev-xymbols/proxy-hosts/dev.xymbols.example").json()[
            "hostnames"
        ]
        == []
    )
    again = client.delete("/api/services/dev-xymbols/proxy-hosts/dev.xymbols.example")
    assert again.status_code == 409
    linked = client.post(
        "/api/services/dev-xymbols/proxy-hosts", json={"hostname": "dev.xymbols.example"}
    )
    assert linked.json()["hostnames"] == ["dev.xymbols.example"]

    # other.example belongs to the root: moving it needs force
    refused = client.post(
        "/api/services/dev-xymbols/proxy-hosts", json={"hostname": "other.example"}
    )
    assert refused.status_code == 409 and "portainer-vm" in refused.text
    moved = client.post(
        "/api/services/dev-xymbols/proxy-hosts", json={"hostname": "other.example", "force": True}
    )
    assert moved.status_code == 200 and set(moved.json()["hostnames"]) == {
        "dev.xymbols.example", "other.example",
    }  # fmt: skip
    assert (
        client.post("/api/services/dev-xymbols/proxy-hosts", json={"hostname": "ghost"}).status_code
        == 404
    )
    assert client.post("/api/services/ghost/proxy-hosts", json={"hostname": "x"}).status_code == 404


# -- CLI -----------------------------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_env(tmp_path, monkeypatch)
    add_host("dev.xymbols.example", "http://10.0.0.9:8081")


def test_cli_suggest_and_adopt(cli_env: None) -> None:
    run = CliRunner()
    out = run.invoke(cli, ["stack", "suggest", "1", "dev-xymbols"])
    assert out.exit_code == 0, out.output
    assert "portainer-vm" in out.output and "database: backup waits for it" in out.output
    adopted = run.invoke(
        cli,
        [
            "stack",
            "adopt",
            "1",
            "dev-xymbols",
            "--host",
            "dev.xymbols.example",
            "--deploy",
            "probes",
            "--webhook",
            "gitlab",
            "--yes",
        ],
    )
    assert adopted.exit_code == 0, adopted.output
    assert "Linked: dev-xymbols, dev-xymbols-db, dev-xymbols-web" in adopted.output
    assert "Webhook secret" in adopted.output and "probes added: 4" in adopted.output
    state = run.invoke(cli, ["service", "state", "dev-xymbols"])
    assert state.exit_code == 0 and "stack dev-xymbols" in state.output


def test_cli_create_delete_and_control(cli_env: None) -> None:
    run = CliRunner()
    out = run.invoke(
        cli,
        ["stack", "create", "1", "newapp", "--git", "https://git.example/app.git",
         "--env", "DB_PASSWORD=s3cret-value", "--git-password", "hunter2", "--yes"],
    )  # fmt: skip
    assert out.exit_code == 0, out.output
    assert "s3cret-value" not in out.output and "hunter2" not in out.output
    assert "***" in out.output and "Created stack newapp" in out.output

    bad = run.invoke(cli, ["stack", "create", "1", "x", "--yes"])
    assert bad.exit_code != 0 and "exactly one" in bad.output

    gone = run.invoke(cli, ["stack", "delete", "1", "media", "--confirm-name", "media"])
    assert gone.exit_code == 0 and "Deleted stack media" in gone.output
    wrong = run.invoke(cli, ["stack", "delete", "1", "media", "--confirm-name", "nope"])
    assert wrong.exit_code != 0 and "type the stack's name" in wrong.output

    assert "stop done for stack media" in run.invoke(cli, ["stack", "stop", "1", "media"]).output
    assert run.invoke(cli, ["stack", "start-container", "1", "media-sonarr-1"]).exit_code == 0


def test_cli_link_and_unlink_host(cli_env: None) -> None:
    run = CliRunner()
    assert run.invoke(cli, ["stack", "adopt", "1", "dev-xymbols", "--yes"]).exit_code == 0
    linked = run.invoke(cli, ["service", "link-host", "dev-xymbols", "dev.xymbols.example"])
    assert linked.exit_code != 0 and "--force" in linked.output  # it is on the root
    forced = run.invoke(
        cli, ["service", "link-host", "dev-xymbols", "dev.xymbols.example", "--force"]
    )
    assert forced.exit_code == 0, forced.output
    assert (
        run.invoke(cli, ["service", "unlink-host", "dev-xymbols", "dev.xymbols.example"]).exit_code
        == 0
    )
    assert (
        run.invoke(cli, ["service", "unlink-host", "dev-xymbols", "dev.xymbols.example"]).exit_code
        != 0
    )
