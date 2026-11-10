"""OR-78: the source pages. All sources, one page per source with its tabs, the sidebar's
Sources section, and the editors that moved off the old Connections page."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator import config as config_module
from orchestrator.connectors import status as source_status
from orchestrator.db import session as session_module
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    source_status.forget()
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c
    source_status.forget()


def _pve(client: TestClient, ident: str, host: str, **kw: Any) -> None:
    r = client.post(
        "/api/connections",
        json={
            "kind": "proxmox",
            "id": ident,
            "name": ident,
            "settings": {"host": host, "token_id": "orchestrator@pve!api"},
            "secrets": {"token_secret": "s3cr3t-" + ident},
            **kw,
        },
    )
    assert r.status_code == 201, r.text


def _proxy(client: TestClient, ident: str) -> None:
    r = client.post(
        "/api/connections",
        json={
            "kind": "proxy",
            "id": ident,
            "name": ident,
            "settings": {"url": "https://traefik-manager.example.lan"},
            "secrets": {"api_key": "proxy-key"},
        },
    )
    assert r.status_code == 201, r.text


def test_the_api_needs_an_admin() -> None:
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with TestClient(create_app()) as c:
        assert c.get("/api/sources").status_code == 401
        r = c.get("/sources", follow_redirects=False)
        assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]


def test_all_sources_lists_every_source_with_its_status(client: TestClient) -> None:
    _pve(client, "cluster-a", "10.0.0.10", default=True)
    _pve(client, "cluster-b", "10.0.1.10")
    _proxy(client, "gateway")
    rows = client.get("/api/sources").json()
    assert [r["id"] for r in rows] == ["cluster-a", "cluster-b", "gateway"]
    a = rows[0]
    assert a["href"] == "/sources/cluster-a" and a["kind_title"] and a["address"] == "10.0.0.10"
    assert a["status"]["state"] == "unknown"  # not checked yet: pages never wait for a check

    checked = client.post("/api/sources/check").json()
    assert all(r["status"]["checked_at"] for r in checked)
    kinds = {k["kind"]: k for k in client.get("/api/sources/kinds").json()}
    assert kinds["proxmox"]["multi"] and kinds["proxmox"]["fields"]
    assert "ntfy" not in kinds and "ssh" not in kinds  # channels and SSH are not sources

    page = client.get("/sources")
    assert page.status_code == 200 and "sources_index.js" in page.text
    assert "s3cr3t" not in page.text and "proxy-key" not in page.text


def test_the_sidebar_folds_a_kind_with_several_sources(client: TestClient) -> None:
    _pve(client, "cluster-a", "10.0.0.10", default=True)
    _pve(client, "cluster-b", "10.0.1.10")
    _proxy(client, "gateway")
    html = client.get("/sources/cluster-b").text
    nav = html[html.index('<nav id="sidebar-nav"') : html.index("</nav>")]
    # Proxmox has two: a fold, open because this page is one of them, both listed under it
    assert 'data-fold="Proxmox" open' in nav
    assert 'href="/sources/cluster-a"' in nav and 'href="/sources/cluster-b"' in nav
    assert 'class="nav-src nav-sub current"' in nav
    # the proxy has one: a single line "Proxy · gateway"
    assert 'data-fold="Proxy"' not in nav and "· gateway" in nav
    assert "/infra/connections" not in nav


def test_a_source_page_has_its_tabs_and_never_its_secrets(client: TestClient) -> None:
    _pve(client, "cluster-a", "10.0.0.10")
    page = client.get("/sources/cluster-a")
    assert page.status_code == 200 and "source.js" in page.text and "conn_drawer.js" in page.text
    assert "s3cr3t" not in page.text
    d = client.get("/api/sources/cluster-a").json()
    assert [t["view"] for t in d["tabs"]] == [
        "overview",
        "resource:nodes",
        "resource:guests",
        "resource:storage",
        "resource:backup_jobs",
        "resource:tasks",
        "settings",
        "raw",
    ]
    assert d["connection"]["secrets_set"]["token_secret"] is True
    assert d["multi"] is True and d["fields"]
    nodes = d["resources"]["nodes"]
    assert nodes["columns"][0]["key"] == "name" and nodes["chips"]
    assert {e["key"] for e in d["raw"]} >= {"cluster", "guests"}
    assert client.get("/sources/nope").status_code == 404
    assert client.get("/api/sources/nope").status_code == 404


def test_overview_resources_and_raw_answer(client: TestClient) -> None:
    _pve(client, "cluster-a", "10.0.0.10")
    ov = client.get("/api/sources/cluster-a/overview").json()
    assert set(ov) == {"tiles", "attention", "facts", "sections"}
    assert all(v != "s3cr3t-cluster-a" for _, v in ov["facts"])
    rows = client.get("/api/sources/cluster-a/resources/nodes").json()
    assert rows["key"] == "nodes" and isinstance(rows["rows"], list)
    assert client.get("/api/sources/cluster-a/resources/nope").status_code == 404
    raw = client.get("/api/sources/cluster-a/raw/version").json()
    assert raw["key"] == "version" and "data" in raw
    assert client.get("/api/sources/cluster-a/raw/nope").status_code == 404
    tested = client.post("/api/sources/cluster-a/test").json()
    assert tested["id"] == "cluster-a" and tested["status"]["checked_at"]


def test_a_switched_off_source_shows_nothing_but_its_settings(client: TestClient) -> None:
    _pve(client, "cluster-a", "10.0.0.10", enabled=False)
    assert client.get("/api/sources/cluster-a").json()["enabled"] is False
    assert client.get("/api/sources/cluster-a/overview").status_code == 409
    assert client.get("/api/sources/cluster-a/resources/nodes").status_code == 409
    row = client.post("/api/sources/cluster-a/test").json()
    assert row["status"]["state"] == "off"


def test_channels_and_ssh_are_edited_where_they_are_used(client: TestClient) -> None:
    notes = client.get("/notifications").text
    assert "conn_drawer.js" in notes and "/infra/connections" not in notes
    system = client.get("/system").text
    assert "conn_drawer.js" in system and 'id="ssh-facts"' in system
    assert "window.ConnDrawer.open" in client.get("/static/notifications.js").text
    assert "window.ConnDrawer.open" in client.get("/static/system.js").text


def _portainer(client: TestClient, ident: str) -> None:
    r = client.post(
        "/api/connections",
        json={
            "kind": "portainer",
            "id": ident,
            "name": ident,
            "settings": {"url": f"https://{ident}.example.lan:9443"},
            "secrets": {"api_key": "ptr-key"},
        },
    )
    assert r.status_code == 201, r.text


def test_a_portainer_has_its_stacks_containers_and_docker_lists(client: TestClient) -> None:
    """OR-79: Containers became tabs of each Portainer's own page; stacks open under it."""
    _portainer(client, "home")
    _portainer(client, "offsite")
    d = client.get("/api/sources/home").json()
    assert [t["key"] for t in d["tabs"]] == [
        "overview",
        "environments",
        "stacks",
        "containers",
        "images",
        "volumes",
        "networks",
        "settings",
        "raw",
    ]
    assert d["resources"]["containers"]["view"] == "portainer.containers"
    page = client.get("/sources/home")
    assert "source_portainer.js" in page.text

    for ident in ("home", "offsite"):
        rows = client.get(f"/api/sources/{ident}/resources/containers").json()["rows"]
        assert rows and all(r["stack_href"].startswith(f"/sources/{ident}/stacks/") for r in rows)
        assert all({"running", "stopped"} & set(r["chips"]) for r in rows)
        stacks = client.get(f"/api/sources/{ident}/resources/stacks").json()["rows"]
        assert stacks and all(s["href"].startswith(f"/sources/{ident}/stacks/") for s in stacks)
    for key in ("images", "volumes", "networks"):
        rows = client.get(f"/api/sources/home/resources/{key}").json()["rows"]
        assert rows and len({r["id"] for r in rows}) == len(rows), key

    stack = stacks[0]
    html = client.get(stack["href"]).text
    assert 'href="/sources/offsite?tab=stacks"' in html and "stack.js" in html
    assert '"instance": "offsite"' in html
    assert client.get("/sources/nope/stacks/1/x").status_code == 404
    _pve(client, "cluster-a", "10.0.0.10")
    assert client.get("/sources/cluster-a/stacks/1/x").status_code == 404  # not a Portainer


def test_two_proxmox_clusters_each_show_and_act_on_their_own(client: TestClient) -> None:
    """OR-80: Nodes & guests became tabs of each Proxmox source; node and guest actions go to
    the cluster that owns the node."""
    from sqlmodel import Session

    from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus, Guest
    from orchestrator.connectors.ownership import proxmox_for_node
    from orchestrator.db.models import Node
    from orchestrator.db.session import get_engine
    from orchestrator.domain.enums import GuestKind

    _pve(client, "cluster-a", "10.0.0.10", default=True)
    _pve(client, "cluster-b", "10.0.1.10")
    adapters = client.app.state.adapters  # type: ignore[attr-defined]
    for ident, node, vmid in (("cluster-a", "node-a", 100), ("cluster-b", "node-c", 200)):
        pve = adapters.for_source(ident)
        pve.simulated_cluster = ClusterStatus(nodes=[ClusterNode(node, True)])
        pve.simulated_guests = [
            Guest(node=node, vmid=vmid, kind=GuestKind.VM, name=f"vm-{vmid}", status="running")
        ]
    with Session(get_engine()) as s:
        s.add(Node(name="node-a"))  # unowned: the default cluster's
        s.add(Node(name="node-c", source_id="cluster-b"))
        s.commit()
        assert proxmox_for_node(s, adapters, "node-c") is adapters.for_source("cluster-b")
        assert proxmox_for_node(s, adapters, "node-a") is adapters.proxmox

    def names(path: str) -> list[str]:
        return [n["name"] for n in client.get(path).json()["nodes"]]

    assert names("/api/views/nodes?source=cluster-a") == ["node-a"]
    assert names("/api/views/nodes?source=cluster-b") == ["node-c"]
    assert names("/api/infra/nodes/status?source=cluster-b") == ["node-c"]
    guests = client.get("/api/infra/proxmox/guests?source=cluster-b").json()
    assert [g["vmid"] for g in guests] == [200]
    node_c = client.get("/api/views/nodes?source=cluster-b").json()["nodes"][0]
    assert node_c["href"] == "/sources/cluster-b?tab=nodes"

    d = client.get("/api/sources/cluster-a").json()
    assert d["resources"]["guests"]["view"] == "proxmox.guests"
    assert "source_proxmox.js" in client.get("/sources/cluster-a").text
    assert client.get("/nodes", follow_redirects=False).status_code == 404


def test_settings_cards_dry_run_and_protected_stacks(client: TestClient) -> None:
    """The Settings tab's Behaviour and Safety cards: fields say which card they are on, Dry run
    makes one source pretend, and a protected stack is only stopped when its name is typed."""
    from orchestrator.adapters.portainer.dry_run import DryRunPortainerAdapter

    _portainer(client, "home")
    d = client.get("/api/sources/home").json()
    cards = {f["name"]: f["card"] for f in d["fields"]}
    assert cards["url"] == "connection" and cards["timeout_s"] == "behaviour"
    assert cards["read_only"] == "safety" and cards["protected"] == "safety"
    assert cards["dry_run"] == "safety"
    assert {f["name"]: f["type"] for f in d["fields"]}["protected"] == "list"
    assert d["last_change"]["actor"]  # adding it was a change

    r = client.put(
        "/api/connections/home",
        json={"settings": {"protected": "dev-xymbols, gateway", "timeout_s": 7}},
    )
    assert r.status_code == 200, r.text
    adapter = client.app.state.adapters.portainer_for("home")  # type: ignore[attr-defined]
    assert isinstance(adapter, DryRunPortainerAdapter)  # DRY_RUN in tests: every one pretends
    assert adapter.protected == frozenset({"dev-xymbols", "gateway"})

    stop = "/api/stacks/1/dev-xymbols/stop?instance=home"
    refused = client.post(stop)
    assert refused.status_code == 409 and "protected" in refused.json()["detail"]
    assert client.post(stop + "&confirm=dev-xymbols").status_code == 200
    assert client.post("/api/stacks/1/dev-xymbols/start?instance=home").status_code == 200
    # a container of a protected stack is protected too
    containers = client.get("/api/sources/home/resources/containers").json()["rows"]
    box = next(c for c in containers if c["stack"] == "dev-xymbols")
    url = f"/api/containers/1/{box['ref']}/restart?instance=home"
    assert client.post(url).status_code == 409
    assert client.post(url + f"&confirm={box['name']}").status_code == 200

    recent = client.get("/api/sources/home/recent").json()
    actions = [x["action"] for x in recent]
    assert "container.restart" in actions and "stack.stop" in actions
    assert "connection.updated" in actions


def test_dry_run_switch_makes_one_source_pretend() -> None:
    from orchestrator.connections.kinds import BY_KIND
    from orchestrator.connections.model import Connection
    from orchestrator.connections.runtime import values_to_settings

    spec = BY_KIND["proxmox"]
    live = Connection(id="a", kind="proxmox", settings={"host": "10.0.0.10"})
    pretend = Connection(id="b", kind="proxmox", settings={"host": "10.0.0.11", "dry_run": True})
    assert values_to_settings(spec, live)["proxmox_adapter"] == "live"
    assert values_to_settings(spec, pretend)["proxmox_adapter"] == "dry_run"


def test_proxmox_storage_backup_jobs_tasks_and_overview(client: TestClient) -> None:
    """The SourceProxmox wireframe: Storage, Backup jobs and Tasks tabs; CPU, Memory and
    Fullest storage tiles; a nodes table and a Recent card on the overview."""
    from orchestrator.adapters.proxmox.base import ClusterNode, ClusterStatus

    _pve(client, "cluster-a", "10.0.0.10")
    pve = client.app.state.adapters.for_source("cluster-a")  # type: ignore[attr-defined]
    pve.simulated_cluster = ClusterStatus(
        nodes=[ClusterNode("node-a", True), ClusterNode("node-b", True)]
    )
    storage = client.get("/api/sources/cluster-a/resources/storage").json()["rows"]
    assert {r["node"] for r in storage} == {"node-a", "node-b"}
    assert all(r["pct"] is not None for r in storage)
    assert not any("full" in r["chips"] for r in storage)  # the fullest is 78%
    jobs = client.get("/api/sources/cluster-a/resources/backup_jobs").json()["rows"]
    assert jobs[0]["what"] == "every guest" and jobs[0]["state"] == "on"
    assert isinstance(client.get("/api/sources/cluster-a/resources/tasks").json()["rows"], list)

    ov = client.get("/api/sources/cluster-a/overview").json()
    labels = [t["label"] for t in ov["tiles"]]
    assert labels == ["Nodes", "Guests", "CPU", "Memory", "Fullest storage"]
    fullest = ov["tiles"][-1]
    assert fullest["value"] == "78%" and fullest["sub"] == "local-lvm on node-b"
    assert fullest["tone"] is None  # under 85%: nothing needs attention
    (nodes,) = ov["sections"]
    assert [r["name"] for r in nodes["rows"]] == ["node-a", "node-b"] and nodes[
        "href"
    ] == "?tab=nodes"
    assert client.get("/api/sources/cluster-a/recent").json()[0]["action"] == "connection.created"
