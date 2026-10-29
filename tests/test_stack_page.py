"""The stack page (``/portainer/<env>/<stack>``) and its API: what is linked, the environment
(secrets hidden, revealing and changing audited by name), Git settings, container actions; the
Containers index; and a parent's state taking in its sub-services."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.db.models import AuditEntry, Service, ServiceState
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import HealthState
from tests import test_stacks as base
from tests.test_stacks import adopt_dev, portainer

client = base.client


def _audit(action: str) -> list[AuditEntry]:
    with Session(get_engine()) as s:
        return list(s.exec(select(AuditEntry).where(AuditEntry.action == action)).all())


def test_the_pages_render(client: TestClient) -> None:
    r = client.get("/portainer/1/dev-xymbols")
    assert r.status_code == 200 and "stack.js" in r.text and "dev-xymbols" in r.text
    loose = client.get("/portainer/1/-")
    assert loose.status_code == 200 and "(no stack)" in loose.text


def test_the_link_lists_the_container_checks(client: TestClient) -> None:
    assert client.get("/api/stacks/1/dev-xymbols/link").json() == {"link": None}
    adopt_dev(client)

    link = client.get("/api/stacks/1/dev-xymbols/link").json()["link"]

    assert link["slug"] == "dev-xymbols" and link["parent"]["slug"] == "portainer-vm"
    checks = {k["container"]: k for k in link["checks"] if k["container"]}
    assert checks["dev-xymbols-db-1"]["backup_waits"] is True
    assert checks["dev-xymbols-web-1"]["backup_waits"] is False


def test_the_environment_hides_secrets_until_revealed(client: TestClient) -> None:
    env = client.get("/api/stacks/1/dev-xymbols/env").json()
    assert env["editable"] is True
    by = {v["name"]: v for v in env["vars"]}
    assert by["DB_PASSWORD"]["secret"] is True and by["DB_PASSWORD"]["value"] is None
    assert "sample-db-password" not in json.dumps(env)
    media = client.get("/api/stacks/1/media/env").json()
    assert {v["name"]: v["value"] for v in media["vars"]} == {"PUID": "1000", "PGID": "1000"}

    r = client.post("/api/stacks/1/dev-xymbols/env/reveal", json={"name": "DB_PASSWORD"})

    assert r.json()["value"] == "sample-db-password"
    entry = _audit("stack.env_revealed")[0]
    assert entry.details == {"variable": "DB_PASSWORD"}
    assert (
        client.post("/api/stacks/1/dev-xymbols/env/reveal", json={"name": "NOPE"}).status_code
        == 404
    )


def test_changing_the_environment_keeps_hidden_values_and_redeploys(client: TestClient) -> None:
    body = {
        "vars": [
            {"name": "DB_PASSWORD", "value": None},  # kept without being revealed
            {"name": "APP_KEY", "value": "base64:new"},
            {"name": "NEW_FLAG", "value": "1"},
        ]
    }

    r = client.put("/api/stacks/1/dev-xymbols/env", json=body)

    assert r.status_code == 200, r.text
    assert r.json() == {
        "change": {"added": ["NEW_FLAG"], "changed": ["APP_KEY"], "removed": ["MAIL_DSN"]},
        "redeployed": True,
    }
    adapter = portainer(client)
    assert dict(adapter.env["dev-xymbols"]) == {
        "DB_PASSWORD": "sample-db-password",
        "APP_KEY": "base64:new",
        "NEW_FLAG": "1",
    }
    assert "env dev-xymbols" in adapter.calls
    entry = _audit("stack.env_changed")[0]
    assert "base64:new" not in json.dumps(entry.details)  # names, never values
    bad = client.put(
        "/api/stacks/1/dev-xymbols/env", json={"vars": [{"name": "1BAD", "value": "x"}]}
    )
    assert bad.status_code == 400
    twice = client.put(
        "/api/stacks/1/dev-xymbols/env",
        json={"vars": [{"name": "A", "value": "1"}, {"name": "A", "value": "2"}]},
    )
    assert twice.status_code == 400


def test_git_settings(client: TestClient) -> None:
    r = client.put(
        "/api/stacks/1/dev-xymbols/git",
        json={"reference": "refs/heads/main", "auto_update_interval": "10m"},
    )
    assert r.status_code == 200, r.text
    stack = client.get("/api/portainer/endpoints/1/stacks/dev-xymbols").json()
    assert stack["git"]["reference"] == "refs/heads/main"
    assert stack["auto_update_interval"] == "10m"
    assert _audit("stack.git_settings")[0].details["auto_update"] == "10m"
    bad = client.put(
        "/api/stacks/1/dev-xymbols/git", json={"reference": "main", "auto_update_interval": "soon"}
    )
    assert bad.status_code == 400
    assert client.put("/api/stacks/1/media/git", json={"reference": "x"}).status_code == 502


def test_container_and_stack_actions(client: TestClient) -> None:
    for verb in ("kill", "recreate"):
        r = client.post(f"/api/containers/1/dev-xymbols-web-1/{verb}")
        assert r.status_code == 200, r.text
    assert client.post("/api/containers/1/dev-xymbols-web-1/explode").status_code == 422
    assert client.post("/api/stacks/1/dev-xymbols/redeploy").status_code == 200
    assert "redeploy dev-xymbols" in portainer(client).calls
    raw = client.get("/api/portainer/endpoints/1/containers/dev-xymbols-db-1/inspect").json()
    assert all(e.endswith("=***") for e in raw["Config"]["Env"])


def test_the_index_has_container_states(client: TestClient) -> None:
    r = client.get("/api/portainer/stacks").json()
    dev = next(s for s in r["stacks"] if s["name"] == "dev-xymbols")
    assert {c["name"] for c in dev["container_states"]} == {
        "dev-xymbols-web-1",
        "dev-xymbols-db-1",
    }
    assert r["loose"] == []  # every sample container is in a compose project


# -- a parent takes in its sub-services --------------------------------------------------------------


def _verdict(slug: str, state: HealthState) -> None:
    with Session(get_engine()) as s:
        svc = s.exec(select(Service).where(Service.slug == slug)).one()
        row = s.get(ServiceState, svc.id) or ServiceState(service_id=svc.id)
        row.last_verdict = state
        row.last_verdict_at = datetime.now(UTC)
        s.add(row)
        s.commit()


def _row(rows: list[dict[str, Any]], slug: str) -> dict[str, Any]:
    return next(r for r in rows if r["slug"] == slug)


def test_a_failing_sub_service_makes_its_healthy_parent_degraded(client: TestClient) -> None:
    adopt_dev(client)
    _verdict("portainer-vm", HealthState.HEALTHY)
    _verdict("dev-xymbols", HealthState.FAILED)

    rows = client.get("/api/views/services").json()["rows"]
    parent = _row(rows, "portainer-vm")
    assert parent["state"]["label"] == "degraded" and parent["sub_failing"] == ["dev-xymbols"]
    stack = _row(rows, "dev-xymbols")
    assert stack["kind"] == "stack" and stack["containers"] == 2
    assert stack["stack"]["href"] == "/portainer/1/dev-xymbols"

    dash = client.get("/api/views/dashboard").json()
    assert dash["services"]["total"] == 1  # the stack counts in its parent
    item = next(a for a in dash["attention"] if a["slug"] == "portainer-vm")
    assert item["kind"] == "degraded" and item["stack"]["href"] == "/portainer/1/dev-xymbols"
    assert not any(a["slug"] == "dev-xymbols" for a in dash["attention"])
    group = next(
        g for g in dash["glance"] if any(x["slug"] == "portainer-vm" for x in g["services"])
    )
    assert [x["slug"] for x in group["services"][0]["subs"]] == ["dev-xymbols"]

    view = client.get("/api/views/service/portainer-vm").json()
    assert [x["slug"] for x in view["subs"]] == ["dev-xymbols"]
    assert view["subs"][0]["backup"] == "waits for dev-xymbols-db-1"

    _verdict("dev-xymbols", HealthState.HEALTHY)
    rows = client.get("/api/views/services").json()["rows"]
    assert _row(rows, "portainer-vm")["state"]["label"] == "healthy"


def test_a_failed_parent_stays_failed(client: TestClient) -> None:
    adopt_dev(client)
    _verdict("portainer-vm", HealthState.FAILED)
    _verdict("dev-xymbols", HealthState.FAILED)
    rows = client.get("/api/views/services").json()["rows"]
    assert _row(rows, "portainer-vm")["state"]["label"] == "failed"


OLD_REGISTRY = """\
version: 1
nodes:
  - {name: pve1, always_on: true}
services:
  - slug: portainer-vm
    name: Portainer VM
    node: pve1
    guest_kind: vm
    guest_id: 101
  - slug: shop
    name: Shop
    parent: portainer-vm
    controller: {type: portainer, endpoint: 1, stack: dev-xymbols}
  - slug: shop-db
    name: db
    parent: shop
    backup_gate: required
    controller: {type: portainer, endpoint: 1, container: dev-xymbols-db-1}
    probes:
      - {name: portainer, kind: portainer, config: {endpoint: 1, container: dev-xymbols-db-1}}
"""


def test_startup_folds_old_container_children_and_saves_the_registry(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from orchestrator import config as config_module
    from orchestrator.db import session as session_module
    from orchestrator.web.app import create_app

    path = tmp_path / "services.yaml"
    path.write_text(OLD_REGISTRY)
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    monkeypatch.setenv("REGISTRY_AUTOSAVE", "true")
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()

    with TestClient(create_app()):
        pass

    text = path.read_text()
    assert "shop-db" not in text and "container:dev-xymbols-db-1" in text
    assert "backup_waits: true" in text
    with Session(get_engine()) as s:
        assert {x.slug for x in s.exec(select(Service)).all()} == {"portainer-vm", "shop"}
