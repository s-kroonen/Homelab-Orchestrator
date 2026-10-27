"""Two Portainers: every call names (or defaults to) the connection it is about."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from orchestrator import config as config_module
from orchestrator.config import get_settings
from orchestrator.connections import runtime
from orchestrator.connections.model import Connection
from orchestrator.connections.store import path_for, write_file
from orchestrator.controllers import validate_controller
from orchestrator.db import session as session_module
from orchestrator.db.models import Node, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin


def _portainer(ident: str, url: str, **kw: Any) -> Connection:
    extra = kw.pop("settings", {})
    return Connection(
        id=ident,
        kind="portainer",
        settings={"url": url, "verify_tls": False, **extra},
        secrets={"api_key": "k"},
        **kw,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    config_module.reset_settings_cache()
    write_file(
        path_for(get_settings()),
        [
            _portainer("home", "https://home.example:9443", site="home"),
            _portainer("offsite", "https://off.example:9443", site="offsite", scope="shared"),
        ],
    )
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_the_page_can_pick_between_connections(client: TestClient) -> None:
    got = client.get("/api/portainer/instances").json()
    assert [(i["id"], i["default"]) for i in got] == [("home", True), ("offsite", False)]
    assert client.get("/api/portainer/stacks").json()["instance"] == "home"
    assert client.get("/api/portainer/stacks?instance=offsite").json()["instance"] == "offsite"
    r = client.get("/api/portainer/stacks?instance=nope")
    assert r.status_code == 404 and "no Portainer connection" in r.json()["detail"]
    assert client.get("/api/portainer/status?instance=offsite").json()["instance"] == "offsite"


def test_a_link_belongs_to_the_connection_it_names(client: TestClient) -> None:
    with Session(get_engine()) as s:
        node = Node(name="eve1", always_on=True)
        s.add(node)
        s.commit()
        s.refresh(node)
        s.add(
            Service(
                slug="off-xymbols",
                name="x",
                node_id=node.id,
                guest_kind=GuestKind.VM,
                guest_id=1,
                controller={
                    "type": "portainer",
                    "endpoint": 1,
                    "stack": "dev-xymbols",
                    "instance": "offsite",
                },
            )
        )
        s.add(
            Service(
                slug="home-xymbols",
                name="y",
                node_id=node.id,
                guest_kind=GuestKind.VM,
                guest_id=2,
                controller={"type": "portainer", "endpoint": 1, "stack": "dev-xymbols"},
            )
        )
        s.commit()

    def linked(instance: str) -> list[str]:
        rows = client.get(f"/api/portainer/stacks?instance={instance}").json()["stacks"]
        return next(r for r in rows if r["name"] == "dev-xymbols")["linked_services"]

    assert linked("home") == ["home-xymbols"]  # no instance on a link = the default
    assert linked("offsite") == ["off-xymbols"]


def test_adopting_on_the_other_connection_is_its_own_thing(client: TestClient) -> None:
    with Session(get_engine()) as s:
        node = Node(name="eve1", always_on=True)
        s.add(node)
        s.commit()
        s.refresh(node)
        s.add(Service(slug="host", name="h", node_id=node.id, guest_kind=GuestKind.VM, guest_id=9))
        s.commit()
    found = client.get("/api/stacks/suggest?endpoint=1&name=dev-xymbols&instance=offsite").json()
    assert found["instance"] == "offsite"
    body = {
        "endpoint_id": 1,
        "stack": "dev-xymbols",
        "slug": "off-dev",
        "parent": "host",
        "instance": "offsite",
        "children": [],
    }
    r = client.post("/api/stacks/adopt", json=body)
    assert r.status_code == 201, r.text
    detail = client.get("/api/registry/services/off-dev").json()
    # not the default connection: the link says which one, probes included
    assert detail["controller"]["instance"] == "offsite"
    assert all(p["config"].get("instance") == "offsite" for p in detail["probes"])
    # the same stack name on the default connection is not "already linked"
    again = client.get("/api/stacks/suggest?endpoint=1&name=dev-xymbols").json()
    assert again["already_linked"] == []
    r = client.post("/api/stacks/adopt", json={**body, "slug": "home-dev", "instance": ""})
    assert r.status_code == 201, r.text
    home = client.get("/api/registry/services/home-dev").json()
    assert "instance" not in home["controller"]  # the default needs no name


def test_a_controller_names_its_connection_or_gets_the_default() -> None:
    cfg = validate_controller(
        {"type": "portainer", "endpoint": 2, "stack": "x", "instance": " off "}
    )
    assert cfg["instance"] == "off"
    assert "instance" not in validate_controller(
        {"type": "portainer", "endpoint": 2, "stack": "x", "instance": ""}
    )


def test_the_default_can_be_named_or_left_unsaid() -> None:
    config_module.reset_settings_cache()
    settings = get_settings()
    write_file(
        path_for(settings),
        [_portainer("a", "https://a:9443"), _portainer("b", "https://b:9443", default=True)],
    )
    config_module.reload_settings()
    assert runtime.default_id(settings, "portainer") == "b"
    assert runtime.instance_of({}, settings) == "b"
    assert runtime.instance_of({"instance": "a"}, settings) == "a"
    assert runtime.find_instance(settings, "portainer", "a").id == "a"  # type: ignore[union-attr]
    assert runtime.find_instance(settings, "portainer", "zzz") is None


async def test_the_controller_talks_to_the_connection_it_names() -> None:
    from orchestrator.controllers.portainer import PortainerController

    config_module.reset_settings_cache()
    settings = get_settings()
    write_file(
        path_for(settings),
        [
            _portainer("home", "https://home.example:9443"),
            _portainer("offsite", "https://off.example:9443", settings={"read_only": True}),
        ],
    )
    config_module.reload_settings()
    default = PortainerController(settings, {"type": "portainer", "endpoint": 1, "stack": "s"})
    off = PortainerController(
        settings, {"type": "portainer", "endpoint": 1, "stack": "s", "instance": "offsite"}
    )
    assert default._base == "https://home.example:9443" and off._base == "https://off.example:9443"
    assert off._read_only is True and "offsite" in off.describe()
    from orchestrator.controllers.base import ControllerError

    with pytest.raises(ControllerError, match="no Portainer connection"):
        PortainerController(
            settings, {"type": "portainer", "endpoint": 1, "stack": "s", "instance": "x"}
        )
