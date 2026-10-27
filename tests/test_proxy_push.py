"""Deferred push of proxy hosts to Traefik Manager (``registry/proxy_push.py``):
sync-state classification, the apply order, rollback, per-host isolation,
pending deletions, Save to YAML / Push now, the status endpoint, the CLI and
the ``file::name`` id handling for Traefik Manager with several config files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import cli_config
from orchestrator.adapters.factory import AdapterBundle
from orchestrator.adapters.proxy.base import ProxyRouteNotFound, RouteInfo, find_route
from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter
from orchestrator.adapters.proxy.traefik_manager import (
    TraefikManagerProxyAdapter,
    _routes_to_route_info,
)
from orchestrator.cli import cli
from orchestrator.db.models import (
    SETTING_PROXY_PENDING_DELETES,
    ProxyHost,
    Service,
    Setting,
)
from orchestrator.db.session import get_engine
from orchestrator.registry import push as push_mod
from orchestrator.registry.proxy_push import (
    PENDING_UPDATE,
    desired_signature,
    evaluate,
)
from orchestrator.registry.push import PushResult, PushStatus
from tests.push_helpers import actions, failures, push, sync_state
from tests.sim_tm import SimTM
from tests.test_proxy_entrypoints_adapter import _patch, _settings
from tests.test_proxy_entrypoints_api import (  # noqa: F401 - dry_client is a fixture
    ROUTE_REF_KEY,
    FakeTM,
    _body,
    _client,
    _rows,
    dry_client,
)
from tests.test_proxy_host_enabled import ToggleTM

REF = "orchestrator-app-example-com"


def _create(client: TestClient, service: Service, **over: Any) -> dict[str, Any]:
    r = client.post("/api/proxy-hosts", json=_body(service, entry_points=["websecure"], **over))
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def _row(host_id: int) -> ProxyHost:
    with Session(get_engine()) as s:
        row = s.get(ProxyHost, host_id)
        assert row is not None
        return row


def _status(client: TestClient) -> dict[str, Any]:
    r = client.get("/api/push/status")
    assert r.status_code == 200, r.text
    (target,) = [t for t in r.json()["targets"] if t["name"] == "proxy"]
    out: dict[str, Any] = target
    return out


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_a_new_host_is_pending_create_then_in_sync_after_a_push(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    host = _create(client, service)
    assert host["sync_state"] == "pending_create"
    assert host["sync_changes"] == ["not created in Traefik Manager yet"]
    assert fake.saves == []

    st = _status(client)
    assert st["reachable"] is True and st["in_sync"] == 0
    assert [(p["hostname"], p["state"]) for p in st["pending"]] == [
        ("app.example.com", "pending_create")
    ]

    assert actions(push(client)) == [("app.example.com", "create")]
    assert sync_state(client, host["id"]) == "in_sync"
    st = _status(client)
    assert st["in_sync"] == 1 and st["pending"] == [] and st["detached"] == []
    assert _row(host["id"]).pushed_sig  # recorded after the success


def test_an_edit_is_pending_update_with_a_note_and_nothing_is_written(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    host = _create(client, service)
    push(client)
    fake.saves.clear()

    r = client.put(
        f"/api/proxy-hosts/{host['id']}", json=_body(service, upstream="http://10.0.0.9:9999")
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sync_state"] == "pending_update" and body["sync_pending"] is True
    assert "edited here since the last push" in body["sync_changes"]
    assert any(
        "upstream differs: Traefik Manager has http://10.0.0.9:8080" in c
        and "we have http://10.0.0.9:9999" in c
        for c in body["sync_changes"]
    )
    assert fake.saves == [] and fake.deletes == []


def test_a_change_made_in_traefik_manager_is_detached_and_push_repairs_it(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    host = _create(client, service)
    push(client)
    fake.routes[REF]["upstream"] = "http://elsewhere:1"  # edited in Traefik Manager's UI
    fake.routes[REF]["eps"] = ["web"]

    row = client.get(f"/api/proxy-hosts/{host['id']}").json()
    assert row["sync_state"] == "detached"
    assert row["sync_changes"] == [
        "upstream differs: Traefik Manager has http://elsewhere:1, we have http://10.0.0.9:8080",
        "entry points differ: Traefik Manager has web, we have websecure",
    ]
    st = _status(client)
    assert [d["hostname"] for d in st["detached"]] == ["app.example.com"] and st["pending"] == []

    fake.saves.clear()
    assert actions(push(client)) == [("app.example.com", "update")]
    assert fake.routes[REF]["upstream"] == "http://10.0.0.9:8080"
    assert fake.routes[REF]["eps"] == ["websecure"]
    assert sync_state(client, host["id"]) == "in_sync"


def test_an_unknown_history_host_is_in_sync_when_what_is_readable_matches(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row from before the signature existed starts empty: it is compared
    with what Traefik Manager has, not assumed to differ."""
    fake = FakeTM()
    fake.seed(REF, "app.example.com", ["websecure"])
    client = _client(monkeypatch, fake)
    with Session(get_engine()) as s:
        ph = ProxyHost(
            hostname="app.example.com",
            upstream="http://1.2.3.4:80",
            service_id=service.id,
            entry_points=["websecure"],
            extra={ROUTE_REF_KEY: REF},
        )
        s.add(ph)
        s.commit()
        s.refresh(ph)
        pid = ph.id
    assert sync_state(client, pid) == "in_sync"
    # ...and differs when it does not.
    fake.routes[REF]["upstream"] = "http://9.9.9.9:80"
    row = client.get(f"/api/proxy-hosts/{pid}").json()
    assert row["sync_state"] == "pending_update"
    assert "upstream differs" in row["sync_changes"][0]


def test_external_routes_only_get_their_enabled_state_pushed(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = ToggleTM()
    fake.seed("hand-made", "pbs.example.com", ["websecure"])
    client = _client(monkeypatch, fake)
    with Session(get_engine()) as s:
        ph = ProxyHost(
            hostname="pbs.example.com",
            upstream="http://changed-here:8006",  # differs from the route: never pushed
            service_id=service.id,
            entry_points=["web"],
        )
        s.add(ph)
        s.commit()
        s.refresh(ph)
        pid = ph.id
    row = client.get(f"/api/proxy-hosts/{pid}").json()
    assert row["sync_state"] == "external" and row["sync_pending"] is False
    assert _status(client)["in_sync"] == 1

    client.post(f"/api/proxy-hosts/{pid}/enabled", json={"enabled": False})
    assert actions(push(client)) == [("pbs.example.com", "disable")]
    assert fake.saves == [] and fake.deletes == []  # content untouched
    assert fake.routes == {} and "hand-made" in fake.disabled
    assert fake.disabled["hand-made"]["upstream"] == "http://1.2.3.4:80"


def test_adopted_routes_are_never_deleted_when_the_host_is_deleted(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    fake.seed("hand-made", "app.example.com", ["websecure"])
    client = _client(monkeypatch, fake)
    r = client.post(
        "/api/proxy-hosts", json=_body(service, upstream="http://1.2.3.4:80", adopt=True)
    )
    assert r.status_code == 201, r.text
    assert r.json()["sync_state"] == "external"
    assert (
        client.delete(f"/api/proxy-hosts/{r.json()['id']}").json()["pending_route_delete"] is False
    )
    assert _status(client)["pending_deletes"] == []
    assert push(client)["results"] == [] and "hand-made" in fake.routes


def test_a_host_that_was_never_pushed_leaves_no_pending_deletion(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, FakeTM())
    host = _create(client, service)
    out = client.delete(f"/api/proxy-hosts/{host['id']}").json()
    assert out["pending_route_delete"] is False
    assert _status(client)["pending_deletes"] == []


def test_local_hosts_are_not_pushed(service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    r = client.post(
        "/api/proxy-hosts", json=_body(service, router_provider="pangolin", upstream="")
    )
    assert r.json()["sync_state"] == "local"
    assert push(client)["results"] == [] and fake.saves == []
    assert _status(client)["in_sync"] == 0  # local hosts are not counted either way


def test_unreachable_traefik_manager_is_reported_not_guessed(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    host = _create(client, service)
    push(client)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    from tests.test_proxy_hosts import _patch_tm_transport

    _patch_tm_transport(monkeypatch, down)
    st = _status(client)
    assert st["reachable"] is False and "could not be read" in st["message"]
    # Our own bookkeeping still says it was pushed as it is now.
    assert sync_state(client, host["id"]) == "in_sync"
    client.put(f"/api/proxy-hosts/{host['id']}", json=_body(service, upstream="http://x:1"))
    result = push(client)
    assert [i["ok"] for i in result["results"]] == [False]
    assert "nothing was pushed" in result["results"][0]["message"]


def test_evaluate_is_pure_and_handles_a_missing_signature() -> None:
    proxy = DryRunProxyAdapter()
    ph = ProxyHost(
        hostname="a.example.com",
        upstream="http://10.0.0.1:80",
        entry_points=["websecure"],
        extra={ROUTE_REF_KEY: "dry-run:a.example.com"},
    )
    route = RouteInfo(
        name="dry-run:a.example.com",
        route_id="dry-run:a.example.com",
        hostnames={"a.example.com"},
        upstream="http://10.0.0.1:80",
        entry_points=("websecure",),
    )
    assert evaluate(proxy, ph, [route]).state == "in_sync"
    ph.pushed_sig = desired_signature(ph)
    ph.upstream = "http://10.0.0.2:80"
    hs = evaluate(proxy, ph, [route])
    assert hs.state == PENDING_UPDATE and hs.actions == ["update"]
    assert evaluate(proxy, ph, None).state == PENDING_UPDATE  # unreadable: not "in sync"


# ---------------------------------------------------------------------------
# Apply: order, rollback, isolation, signature
# ---------------------------------------------------------------------------


def test_apply_order_is_enable_then_update_and_disable_last(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    host = _create(client, service)
    push(client)

    # Parked in Traefik Manager, wanted on and edited: switch on, THEN rewrite.
    tm.disabled[REF] = tm.routes.pop(REF)
    client.put(f"/api/proxy-hosts/{host['id']}", json=_body(service, upstream="http://10.0.0.9:7"))
    result = push(client)
    assert actions(result) == [("app.example.com", "enable"), ("app.example.com", "update")]
    assert REF in tm.routes and tm.routes[REF]["upstream"] == "http://10.0.0.9:7"

    # Wanted off and edited: rewrite, THEN switch off (so the edit never lands on
    # a half-state and the final state is always "off").
    client.put(f"/api/proxy-hosts/{host['id']}", json=_body(service, upstream="http://10.0.0.9:8"))
    client.post(f"/api/proxy-hosts/{host['id']}/enabled", json={"enabled": False})
    result = push(client)
    assert actions(result) == [("app.example.com", "update"), ("app.example.com", "disable")]
    assert REF in tm.disabled and tm.disabled[REF]["upstream"] == "http://10.0.0.9:8"


def test_a_rejected_create_is_rolled_back_and_does_not_stop_the_other_hosts(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    good = _create(client, service, hostname="good.example.com")
    bad = _create(client, service, hostname="bad.example.com")
    # Somebody else grabs bad.example.com in Traefik Manager before the push.
    fake.seed("squatter", "bad.example.com", ["websecure"])

    result = push(client)
    assert actions(result) == [("good.example.com", "create")]
    (fail,) = failures(result)
    assert fail["hostname"] == "bad.example.com" and "Refusing to overwrite" in fail["message"]
    assert "squatter" in fake.routes and "orchestrator-bad-example-com" not in fake.routes
    assert sync_state(client, good["id"]) == "in_sync"
    assert _row(bad["id"]).pushed_sig == ""  # only recorded on success


def test_signature_is_recorded_only_after_a_successful_push(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = ToggleTM()
    client = _client(monkeypatch, tm)
    host = _create(client, service)
    push(client)
    sig = _row(host["id"]).pushed_sig
    assert sig and sig.endswith(":1")

    client.post(f"/api/proxy-hosts/{host['id']}/enabled", json={"enabled": False})
    tm.fail_toggle.add(REF)
    assert failures(push(client))
    assert _row(host["id"]).pushed_sig == sig
    tm.fail_toggle.clear()
    assert not failures(push(client))
    assert _row(host["id"]).pushed_sig.endswith(":0") and _row(host["id"]).pushed_sig != sig


def test_hostnames_filter_and_dry_run_plan(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    _create(client, service, hostname="a.example.com")
    _create(client, service, hostname="b.example.com")

    plan = push(client, dry_run=True)
    assert plan["dry_run"] is True
    assert [(i["hostname"], i["action"], i["planned"]) for i in plan["results"]] == [
        ("a.example.com", "create", True),
        ("b.example.com", "create", True),
    ]
    assert fake.saves == []  # a plan writes nothing

    result = push(client, hostnames=["B.example.com"])
    assert actions(result) == [("b.example.com", "create")]
    assert [s["serviceName"] for s in fake.saves] == ["orchestrator-b-example-com"]


def test_push_endpoint_rejects_unknown_targets(dry_client: TestClient) -> None:  # noqa: F811
    r = dry_client.post("/api/push", json={"targets": ["nope"]})
    assert r.status_code == 422 and "nope" in r.text


# ---------------------------------------------------------------------------
# Pending deletions
# ---------------------------------------------------------------------------


def test_recreating_a_deleted_host_takes_its_route_over(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    host = _create(client, service)
    push(client)
    client.delete(f"/api/proxy-hosts/{host['id']}")
    assert len(_status(client)["pending_deletes"]) == 1

    again = _create(client, service)  # same hostname: not a conflict with its own old route
    assert again["extra"][ROUTE_REF_KEY] == REF
    assert _status(client)["pending_deletes"] == []
    fake.saves.clear()
    push(client)
    assert fake.deletes == []  # nothing is deleted behind its back
    assert REF in fake.routes


def test_switching_a_host_away_from_traefik_removes_its_route_on_push(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    host = _create(client, service)
    push(client)
    r = client.put(
        f"/api/proxy-hosts/{host['id']}",
        json=_body(service, router_provider="pangolin", upstream=""),
    )
    assert r.status_code == 200, r.text
    assert [d["route_ref"] for d in _status(client)["pending_deletes"]] == [REF]
    assert actions(push(client)) == [("app.example.com", "delete")]
    assert fake.routes == {}


def test_reset_to_saved_clears_pending_deletions_and_keeps_what_was_pushed(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    host = _create(client, service)
    saved = client.post("/api/registry/save")
    assert saved.status_code == 200, saved.text
    assert actions({"results": saved.json()["push_results"]}) == [("app.example.com", "create")]

    client.delete(f"/api/proxy-hosts/{host['id']}")
    assert len(_status(client)["pending_deletes"]) == 1
    assert client.post("/api/registry/reset").status_code == 200
    assert _status(client)["pending_deletes"] == []
    with Session(get_engine()) as s:
        assert s.get(Setting, SETTING_PROXY_PENDING_DELETES) is None
    (back,) = client.get("/api/proxy-hosts").json()
    # The YAML put the host back; what was pushed for it is remembered, so it
    # is simply in sync - no create, no delete.
    assert back["sync_state"] == "in_sync"
    assert push(client)["results"] == [] and fake.deletes == []


# ---------------------------------------------------------------------------
# Save to YAML
# ---------------------------------------------------------------------------


def test_save_to_yaml_writes_the_yaml_then_pushes_and_reports(
    service: Service, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    _create(client, service)
    assert fake.saves == []

    r = client.post("/api/registry/save")
    assert r.status_code == 200, r.text
    body = r.json()
    # Existing keys unchanged...
    assert body["status"] == "saved" and body["sha256"] and body["path"].endswith("services.yaml")
    # ...push keys are additive.
    assert body["push"] == {"attempted": 1, "ok": 1, "failed": 0}
    (item,) = body["push_results"]
    assert (item["target"], item["hostname"], item["action"], item["ok"]) == (
        "proxy",
        "app.example.com",
        "create",
        True,
    )
    assert (tmp_path / "services.yaml").exists() and len(fake.saves) == 1
    # Saving the pushed state again changes nothing in the registry.
    assert client.get("/api/registry/status").json()["dirty"] is False


def test_yaml_is_saved_even_when_the_push_fails(
    service: Service, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeTM()
    client = _client(monkeypatch, fake)
    _create(client, service)

    from tests.test_proxy_hosts import _patch_tm_transport

    _patch_tm_transport(monkeypatch, lambda request: httpx.Response(500, json={"ok": False}))
    r = client.post("/api/registry/save")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (tmp_path / "services.yaml").exists()
    assert body["push"]["failed"] == 1 and body["push"]["ok"] == 0
    assert not body["push_results"][0]["ok"]
    assert len(_rows()) == 1  # nothing lost; still pending
    assert client.get("/api/registry/status").json()["dirty"] is False


def test_a_crashing_pusher_never_fails_save_to_yaml(
    service: Service, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client(monkeypatch, FakeTM())
    _create(client, service)

    async def boom(*_a: Any, **_k: Any) -> list[PushResult]:
        raise RuntimeError("kaboom")

    from orchestrator.registry.proxy_push import ProxyPusher

    monkeypatch.setattr(ProxyPusher, "apply", boom)
    r = client.post("/api/registry/save")
    assert r.status_code == 200, r.text
    (item,) = r.json()["push_results"]
    assert item["ok"] is False and "kaboom" in item["message"]
    assert (tmp_path / "services.yaml").exists()


class _CustomPusher:
    name = "custom"

    def __init__(self) -> None:
        self.applied = 0

    async def status(self, session: Session, adapters: AdapterBundle) -> PushStatus:
        return PushStatus(name="custom", in_sync=3)

    async def apply(
        self,
        session: Session,
        adapters: AdapterBundle,
        only: Any = None,
        *,
        dry_run: bool = False,
        actor: str = "system",
    ) -> list[PushResult]:
        self.applied += 1
        return [PushResult("custom", "thing", "sync", True, "done")]


def test_other_services_can_register_a_pusher(dry_client: TestClient) -> None:  # noqa: F811
    custom = _CustomPusher()
    push_mod.register_pusher(custom)
    try:
        targets = {t["name"]: t for t in dry_client.get("/api/push/status").json()["targets"]}
        assert set(targets) == {"proxy", "custom"} and targets["custom"]["in_sync"] == 3
        r = dry_client.post("/api/registry/save")
        assert {i["target"] for i in r.json()["push_results"]} == {"custom"}
        only = dry_client.post("/api/push", json={"targets": ["custom"]}).json()
        assert only["summary"] == {"attempted": 1, "ok": 1, "failed": 0}
        assert custom.applied == 2
    finally:
        push_mod.PUSHERS[:] = [p for p in push_mod.PUSHERS if p.name != "custom"]


# ---------------------------------------------------------------------------
# Traefik Manager with several config files: `file::name` ids
# ---------------------------------------------------------------------------

MULTI_REF = "orchestrator-app-example-com"


def test_routes_keep_the_id_and_the_config_file() -> None:
    (info,) = _routes_to_route_info(
        [
            {
                "id": "apps.yml::web",
                "name": "web",
                "protocol": "http",
                "rule": "Host(`a.example.com`)",
                "configFile": "apps.yml",
            }
        ]
    )
    assert (info.name, info.route_id, info.config_file, info.ref) == (
        "web",
        "apps.yml::web",
        "apps.yml",
        "apps.yml::web",
    )
    assert find_route([info], "web").route_id == "apps.yml::web"  # type: ignore[union-attr]
    assert find_route([info], "apps.yml::web") is info
    assert find_route([info], "nope") is None


def test_find_route_prefers_the_file_whose_rule_matches_the_host() -> None:
    def route(file: str, host: str) -> RouteInfo:
        return RouteInfo(
            name="web",
            route_id=f"{file}::web",
            hostnames={host},
            upstream=None,
            config_file=file,
        )

    routes = [route("dynamic.yml", "a.example.com"), route("apps.yml", "b.example.com")]
    assert find_route(routes, "web", "b.example.com").route_id == "apps.yml::web"  # type: ignore[union-attr]
    assert find_route(routes, "web", "a.example.com").route_id == "dynamic.yml::web"  # type: ignore[union-attr]
    assert find_route(routes, "apps.yml::web", "a.example.com").route_id == "apps.yml::web"  # type: ignore[union-attr]


async def test_toggle_resolves_a_bare_name_to_the_real_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug: the stored reference is the bare router name, Traefik Manager
    only acts on `apps.yml::<name>` and otherwise answers 200 to a no-op."""
    sim = SimTM()
    route_id = sim.add("apps.yml", MULTI_REF, "app.example.com")
    _patch(monkeypatch, sim)
    adapter = TraefikManagerProxyAdapter(_settings())

    await adapter.set_route_enabled(MULTI_REF, False)
    assert sim.calls("toggle") == [False]
    assert [rid for k, rid, _ in sim.requests if k == "toggle"] == [route_id]
    assert sim.is_enabled(route_id) is False

    await adapter.set_route_enabled(MULTI_REF, True)  # needs the disabled-store key == id
    assert sim.is_enabled(route_id) is True


async def test_a_toggle_that_traefik_manager_ignored_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = SimTM()
    sim.add("apps.yml", MULTI_REF, "app.example.com")
    sim.ignore_toggle = True  # 200, but nothing happened
    _patch(monkeypatch, sim)
    adapter = TraefikManagerProxyAdapter(_settings())
    with pytest.raises(Exception, match="accepted the request but the route is still enabled"):
        await adapter.set_route_enabled(MULTI_REF, False)

    sim.ignore_toggle = False
    await adapter.set_route_enabled(MULTI_REF, False)
    sim.ignore_toggle = True
    with pytest.raises(Exception, match="accepted the request but the route is still disabled"):
        await adapter.set_route_enabled(MULTI_REF, True)


async def test_the_redirect_companion_is_read_back_too(monkeypatch: pytest.MonkeyPatch) -> None:
    sim = SimTM()
    sim.add("apps.yml", MULTI_REF, "app.example.com")
    comp = sim.add("apps.yml", f"{MULTI_REF}-redirect", "app.example.com", eps=("web",))

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"{MULTI_REF}/toggle"):  # the main toggle is ignored
            return httpx.Response(200, json={"ok": True})
        return sim(request)

    _patch(monkeypatch, handle)
    adapter = TraefikManagerProxyAdapter(_settings())
    with pytest.raises(Exception, match="still enabled"):
        await adapter.set_route_enabled(MULTI_REF, False, with_redirect=True)
    # The companion that was switched off first is put back.
    assert sim.is_enabled(comp) is True


async def test_unknown_route_is_not_a_silent_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, SimTM())
    adapter = TraefikManagerProxyAdapter(_settings())
    with pytest.raises(ProxyRouteNotFound):
        await adapter.set_route_enabled("nothing", False)


async def test_same_name_in_two_files_is_told_apart_by_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = SimTM()
    mine = sim.add("apps.yml", "web", "b.example.com")
    other = sim.add("dynamic.yml", "web", "a.example.com")
    _patch(monkeypatch, sim)
    adapter = TraefikManagerProxyAdapter(_settings())
    await adapter.set_route_enabled("web", False, hostname="b.example.com")
    assert sim.is_enabled(mine) is False and sim.is_enabled(other) is True


async def test_update_edits_the_route_in_its_own_file_instead_of_duplicating_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = SimTM()
    sim.add("apps.yml", MULTI_REF, "app.example.com")
    _patch(monkeypatch, sim)
    adapter = TraefikManagerProxyAdapter(_settings())
    await adapter.update_route(
        MULTI_REF,
        hostname="app.example.com",
        upstream="http://10.0.0.5:9090",
        entry_points=["websecure"],
        cert_resolver="",
    )
    (form,) = sim.calls("save")
    assert form["originalId"] == f"apps.yml::{MULTI_REF}" and form["configFile"] == "apps.yml"
    assert form["serviceName"] == MULTI_REF and form["isEdit"] == "true"
    assert sim.files["apps.yml"][MULTI_REF]["upstream"] == "http://10.0.0.5:9090"
    assert sim.files["dynamic.yml"] == {}  # no second copy in the default file


async def test_delete_names_the_file_so_a_same_named_router_elsewhere_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = SimTM()
    sim.add("dynamic.yml", "web", "a.example.com")
    sim.add("apps.yml", "web", "b.example.com")
    _patch(monkeypatch, sim)
    adapter = TraefikManagerProxyAdapter(_settings())
    await adapter.delete_route("web", with_redirect=False, hostname="b.example.com")
    assert sim.calls("delete") == ["apps.yml"]  # the configFile form field
    assert [rid for k, rid, _ in sim.requests if k == "delete"] == ["apps.yml::web"]
    assert "web" in sim.files["dynamic.yml"] and "web" not in sim.files["apps.yml"]


def test_end_to_end_disable_and_enable_reach_a_multi_file_traefik_manager(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM()
    client = _client(monkeypatch, sim, TRAEFIK_MANAGER_CONFIG_FILE="")
    host = _create(client, service)
    push(client)
    route_id = f"dynamic.yml::{MULTI_REF}"  # created into the default file
    assert sim.is_enabled(route_id) is True

    client.post(f"/api/proxy-hosts/{host['id']}/enabled", json={"enabled": False})
    assert actions(push(client)) == [("app.example.com", "disable")]
    assert sim.is_enabled(route_id) is False

    client.post(f"/api/proxy-hosts/{host['id']}/enabled", json={"enabled": True})
    assert actions(push(client)) == [("app.example.com", "enable")]
    assert sim.is_enabled(route_id) is True
    assert sync_state(client, host["id"]) == "in_sync"

    # A toggle Traefik Manager silently ignores is a failed push item, not a success.
    sim.ignore_toggle = True
    client.post(f"/api/proxy-hosts/{host['id']}/enabled", json={"enabled": False})
    (fail,) = failures(push(client))
    assert "accepted the request but the route is still enabled" in fail["message"]
    assert sync_state(client, host["id"]) == "pending_update"


def test_a_route_in_another_file_is_updated_in_place_by_a_push(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM()
    sim.add("apps.yml", MULTI_REF, "app.example.com", upstream="http://10.0.0.9:8080")
    client = _client(monkeypatch, sim)
    with Session(get_engine()) as s:
        ph = ProxyHost(
            hostname="app.example.com",
            upstream="http://10.0.0.9:9999",
            service_id=service.id,
            entry_points=["websecure"],
            extra={ROUTE_REF_KEY: MULTI_REF},
        )
        s.add(ph)
        s.commit()
    assert actions(push(client)) == [("app.example.com", "update")]
    assert sim.files["apps.yml"][MULTI_REF]["upstream"] == "http://10.0.0.9:9999"
    assert sim.files["dynamic.yml"] == {}


# ---------------------------------------------------------------------------
# Migration + loader
# ---------------------------------------------------------------------------


def test_migration_0015_adds_pushed_sig_empty_by_default() -> None:
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    from orchestrator.config import get_settings

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, "0014_service_ip")
    engine = sa.create_engine(get_settings().database_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO proxy_host (hostname, upstream, router_provider, extra, "
                "wake_on_hit, entry_points, cert_resolver, route_options, enabled) "
                "VALUES ('a.example.com', '', 'TRAEFIK', '{}', 0, '[]', '', '{}', 1)"
            )
        )
    command.upgrade(cfg, "0015_proxy_host_pushed_sig")
    with engine.connect() as conn:
        assert "pushed_sig" in {c["name"] for c in sa.inspect(conn).get_columns("proxy_host")}
        assert conn.execute(sa.text("SELECT pushed_sig FROM proxy_host")).scalar_one() == ""
    command.downgrade(cfg, "0014_service_ip")
    with engine.connect() as conn:
        assert "pushed_sig" not in {c["name"] for c in sa.inspect(conn).get_columns("proxy_host")}
    engine.dispose()


def test_pushed_sig_is_not_part_of_the_yaml(
    service: Service, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client(monkeypatch, FakeTM())
    _create(client, service)
    client.post("/api/registry/save")
    assert "pushed_sig" not in (tmp_path / "services.yaml").read_text()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _patch_cli_adapter(monkeypatch: pytest.MonkeyPatch, adapter: DryRunProxyAdapter) -> None:
    def _fake_build_adapters(settings: Any = None) -> AdapterBundle:
        from orchestrator.adapters.notifier.null import NullNotifier
        from orchestrator.adapters.pbs.dry_run import DryRunPbsAdapter
        from orchestrator.adapters.power.dry_run import DryRunPowerAdapter
        from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter

        return AdapterBundle(
            power=DryRunPowerAdapter(),
            proxmox=DryRunProxmoxAdapter(),
            pbs=DryRunPbsAdapter(),
            proxy=adapter,
            notifier=NullNotifier(),
        )

    monkeypatch.setattr(cli_config, "build_adapters", _fake_build_adapters)


def test_cli_proxy_status_and_push(
    session: Session, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = DryRunProxyAdapter()
    _patch_cli_adapter(monkeypatch, adapter)
    session.add(
        ProxyHost(
            hostname="cli.example.com",
            upstream="http://10.0.0.4:80",
            service_id=service.id,
            entry_points=["websecure"],
            extra={ROUTE_REF_KEY: "dry-run:cli.example.com"},
        )
    )
    session.commit()

    status = CliRunner().invoke(cli, ["proxy", "status"])
    assert status.exit_code == 1, status.output
    assert "cli.example.com" in status.output and "pending_create" in status.output

    pushed = CliRunner().invoke(cli, ["proxy", "push", "--yes"])
    assert pushed.exit_code == 0, pushed.output
    assert "cli.example.com" in pushed.output and "create" in pushed.output
    assert "dry-run:cli.example.com" in adapter._specs

    status = CliRunner().invoke(cli, ["proxy", "status"])
    assert status.exit_code == 0, status.output
    assert "1 in sync, 0 pending" in status.output
    session.expire_all()
    (ph,) = session.exec(select(ProxyHost)).all()
    assert ph.pushed_sig


def test_cli_proxy_push_exits_1_when_an_item_fails(
    session: Session, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Failing(DryRunProxyAdapter):
        async def create_route(self, **kwargs: Any) -> str:
            raise RuntimeError("gateway said no")

    _patch_cli_adapter(monkeypatch, Failing())
    session.add(
        ProxyHost(
            hostname="x.example.com",
            upstream="http://10.0.0.4:80",
            service_id=service.id,
            entry_points=["websecure"],
        )
    )
    session.commit()
    result = CliRunner().invoke(cli, ["proxy", "push", "--yes"])
    assert result.exit_code == 1, result.output
    assert "gateway said no" in result.output and "1 failed" in result.output


def test_every_save_button_surfaces_the_push_outcome(dry_client: TestClient) -> None:  # noqa: F811
    # What is not pushed shows in one place: the pending pill in the header, on every page.
    for path in ("/proxy-hosts", "/", "/backup-policies"):
        assert 'id="pending-pill"' in dry_client.get(path).text
    shell = dry_client.get("/static/shell.js").text
    assert "/api/pending/retry" in shell and "Save and push" in shell
    # Pages not rebuilt yet keep their Save to YAML, which also pushes.
    for path in ("/", "/backup-policies"):
        assert "PushUI.save" in dry_client.get(path).text
    js = dry_client.get("/static/push.js")
    assert js.status_code == 200 and "push_results" in js.text
    # The proxy host list offers a push for one host; the drawer says Save pushes.
    assert "Push now" in dry_client.get("/static/proxy_hosts.js").text
    assert "pushes it to Traefik Manager" in dry_client.get("/static/objects.js").text
