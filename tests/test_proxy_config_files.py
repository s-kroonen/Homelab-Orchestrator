"""Which Traefik Manager config file a proxy host's route is created in.

Runs against ``tests.sim_tm.SimTM``, which reproduces what the real app does
with ``configFile``: it uses a file it knows, CREATES an unknown one only when
it runs with ``CONFIG_DIR``, and otherwise silently falls back to its DEFAULT
file (``_resolve_config_path`` returning ``''``) - exactly what must never
happen unnoticed.
"""

# ruff: noqa: F811 - imported pytest fixtures are re-declared as test arguments
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session, select

from orchestrator.adapters.proxy.base import ConfigFiles, RouteOptions
from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter
from orchestrator.adapters.proxy.traefik_manager import TraefikManagerProxyAdapter
from orchestrator.config import Settings
from orchestrator.db.models import ProxyHost, Service
from orchestrator.db.session import get_engine
from orchestrator.registry.config_files import (
    check_template,
    effective_file,
    normalize_config_file,
    resolve_config_file,
    unknown_file_problem,
)
from orchestrator.registry.loader import read_yaml_file
from orchestrator.registry.proxy_push import desired_signature
from orchestrator.registry.schema import ProxyHostSpec
from tests.push_helpers import actions, failures, push
from tests.sim_tm import DEFAULT_FILE, SimTM
from tests.test_proxy_entrypoints_adapter import _patch, _settings
from tests.test_proxy_entrypoints_api import _body, _client, dry_client  # noqa: F401

REF = "orchestrator-app-example-com"


def _create(client: TestClient, service: Service, **over: Any) -> dict[str, Any]:
    r = client.post(
        "/api/proxy-hosts", json=_body(service, **{"entry_points": ["websecure"], **over})
    )
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def _saves(sim: SimTM) -> list[dict[str, str]]:
    return [dict(form) for form in sim.calls("save")]


def _get(client: TestClient, host_id: int) -> dict[str, Any]:
    out: dict[str, Any] = client.get(f"/api/proxy-hosts/{host_id}").json()
    return out


# ---------------------------------------------------------------------------
# Pure resolution
# ---------------------------------------------------------------------------


def test_default_template_is_one_file_per_service() -> None:
    assert Settings().traefik_manager_config_file == "{service}.yml"
    assert (
        resolve_config_file("{service}.yml", service_slug="media", hostname="a.b.nl") == "media.yml"
    )


def test_template_placeholders_and_override_and_blank() -> None:
    kw: dict[str, Any] = {"service_slug": "media", "hostname": "Plex.Example.com"}
    assert resolve_config_file("{hostname}.yml", **kw) == "plex-example-com.yml"
    assert resolve_config_file("{service}-{hostname}", **kw) == "media-plex-example-com.yml"
    assert resolve_config_file("{service}.yml", override="custom", **kw) == "custom.yml"
    assert resolve_config_file("", **kw) == ""  # blank: Traefik Manager's default file
    assert resolve_config_file("{service}.yml", override="x.yaml", **kw) == "x.yaml"


@pytest.mark.parametrize("bad", ["a/b.yml", "../x.yml", "sub\\x.yml", "-lead.yml", "sp ace.yml"])
def test_normalize_refuses_anything_but_a_plain_file_name(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_config_file(bad)


def test_templates_are_validated_when_settings_load() -> None:
    assert check_template("") == ""
    with pytest.raises(ValueError):
        check_template("{nope}.yml")
    with pytest.raises(ValidationError):
        Settings(traefik_manager_config_file="{nope}.yml")
    assert Settings(traefik_manager_config_file="").traefik_manager_config_file == ""


def test_unknown_file_problem_and_effective_file() -> None:
    fixed = ConfigFiles(files=("dynamic.yml", "apps.yml"), can_create=False, default="dynamic.yml")
    dirmode = ConfigFiles(files=("_shared.yml",), can_create=True)
    one = ConfigFiles(files=("dynamic.yml",), can_create=False)
    msg = unknown_file_problem("thing.yml", fixed)
    assert msg and "CONFIG_PATHS" in msg and "dynamic.yml, apps.yml" in msg and "thing.yml" in msg
    assert unknown_file_problem("apps.yml", fixed) is None
    assert unknown_file_problem("thing.yml", dirmode) is None  # TM creates it
    assert unknown_file_problem("thing.yml", None) is None  # unreadable: nothing to check
    assert unknown_file_problem("thing.yml", ConfigFiles(can_create=None)) is None
    # A single fixed file: the template has nothing to choose, an explicit name is checked.
    assert effective_file("thing.yml", one, explicit=False) == ("", None)
    assert effective_file("thing.yml", one, explicit=True)[1] is not None
    assert effective_file("", fixed, explicit=False) == ("", None)


# ---------------------------------------------------------------------------
# Create posts the resolved file
# ---------------------------------------------------------------------------


def test_create_posts_the_default_template_file(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("_shared.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    _create(client, service)
    assert actions(push(client)) == [("app.example.com", "create")]
    (save,) = _saves(sim)
    assert save["configFile"] == "thing.yml" and save["isEdit"] == "false"
    assert sim.file_of(REF) == "thing.yml"  # TM created the file


def test_create_posts_a_custom_template_file(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("_shared.yml",), config_dir=True)
    client = _client(monkeypatch, sim, TRAEFIK_MANAGER_CONFIG_FILE="{service}-{hostname}.yml")
    _create(client, service)
    push(client)
    assert _saves(sim)[0]["configFile"] == "thing-app-example-com.yml"


def test_create_posts_the_per_host_override_over_the_template(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("_shared.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    host = _create(client, service, route_options={"config_file": "special"})
    assert host["route_options"]["config_file"] == "special.yml"  # normalised
    assert host["config_file_resolved"] == "special.yml"
    push(client)
    assert _saves(sim)[0]["configFile"] == "special.yml"
    assert sim.file_of(REF) == "special.yml"


def test_blank_template_posts_no_file_and_lands_in_the_managers_default(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM()
    client = _client(monkeypatch, sim, TRAEFIK_MANAGER_CONFIG_FILE="")
    _create(client, service)
    push(client)
    assert _saves(sim)[0]["configFile"] == ""
    assert sim.file_of(REF) == DEFAULT_FILE


def test_the_redirect_companion_goes_into_the_same_file_as_its_main_route(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("_shared.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    _create(
        client, service, entry_points=["web", "websecure"], route_options={"http_redirect": True}
    )
    assert failures(push(client)) == []
    main, companion = _saves(sim)
    assert (main["serviceName"], main["configFile"]) == (REF, "thing.yml")
    assert (companion["serviceName"], companion["configFile"]) == (f"{REF}-redirect", "thing.yml")
    assert sim.copies_of(REF) == ["thing.yml"] and sim.copies_of(f"{REF}-redirect") == ["thing.yml"]


# ---------------------------------------------------------------------------
# A file Traefik Manager would silently replace is refused, never posted
# ---------------------------------------------------------------------------


def test_override_naming_a_file_the_manager_cannot_write_is_refused_by_the_form(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("dynamic.yml", "apps.yml"))  # CONFIG_PATHS only
    client = _client(monkeypatch, sim)
    r = client.post(
        "/api/proxy-hosts",
        json=_body(service, entry_points=["websecure"], route_options={"config_file": "nope.yml"}),
    )
    assert r.status_code == 422, r.text
    message = r.json()["detail"]["errors"]["config_file"]
    assert (
        "CONFIG_PATHS" in message and "dynamic.yml, apps.yml" in message and "nope.yml" in message
    )
    ok = client.post(
        "/api/proxy-hosts",
        json=_body(service, entry_points=["websecure"], route_options={"config_file": "apps.yml"}),
    )
    assert ok.status_code == 201, ok.text


def test_template_file_the_manager_does_not_have_fails_the_push_and_writes_nothing(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("dynamic.yml", "apps.yml"))
    client = _client(monkeypatch, sim)
    _create(client, service)  # template -> thing.yml: unknown, cannot be created
    result = push(client)
    (failure,) = failures(result)
    assert failure["action"] == "create"
    assert "thing.yml" in failure["message"] and "CONFIG_PATHS" in failure["message"]
    assert "dynamic.yml, apps.yml" in failure["message"]
    assert _saves(sim) == []  # nothing fell back to the default file
    assert sim.copies_of(REF) == []


def test_a_manager_with_one_fixed_file_has_nothing_to_choose_so_the_template_is_skipped(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("dynamic.yml",), multi=False)
    client = _client(monkeypatch, sim)
    _create(client, service)
    assert failures(push(client)) == []
    assert _saves(sim)[0]["configFile"] == ""
    assert sim.file_of(REF) == "dynamic.yml"
    # ...but a name the operator typed is still checked.
    r = client.post(
        "/api/proxy-hosts",
        json=_body(
            service,
            hostname="b.example.com",
            entry_points=["websecure"],
            route_options={"config_file": "other.yml"},
        ),
    )
    assert r.status_code == 422 and "config_file" in r.json()["detail"]["errors"]


def test_a_file_added_to_the_manager_a_moment_ago_is_accepted(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("dynamic.yml",))
    client = _client(monkeypatch, sim)
    client.get("/api/proxy/entrypoints")  # fills the 30 s cache
    sim.files["fresh.yml"] = {}
    r = client.post(
        "/api/proxy-hosts",
        json=_body(service, entry_points=["websecure"], route_options={"config_file": "fresh.yml"}),
    )
    assert r.status_code == 201, r.text


def test_the_adapter_itself_refuses_when_asked_to_write_to_an_unwritable_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim = SimTM(files=("dynamic.yml",), config_dir=False)
    _patch(monkeypatch, sim)
    adapter = TraefikManagerProxyAdapter(_settings())

    import asyncio

    with pytest.raises(ValueError, match="CONFIG_PATHS"):
        asyncio.run(
            adapter.create_route(
                hostname="a.example.com",
                upstream="http://10.0.0.9:80",
                entry_points=["websecure"],
                config_file="ghost.yml",
            )
        )
    assert _saves(sim) == []


# ---------------------------------------------------------------------------
# Existing routes stay where they are
# ---------------------------------------------------------------------------


def test_an_update_keeps_the_route_in_the_file_it_lives_in(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("_shared.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    host = _create(client, service, route_options={"config_file": "_shared.yml"})
    push(client)
    assert sim.file_of(REF) == "_shared.yml"

    # The wanted file changes AND the content is edited: the content push edits the
    # route where it is - it never moves it behind the operator's back.
    r = client.put(
        f"/api/proxy-hosts/{host['id']}",
        json=_body(
            service,
            entry_points=["websecure"],
            upstream="http://10.0.0.9:9999",
            route_options={"config_file": "thing.yml"},
        ),
    )
    assert r.status_code == 200, r.text
    assert actions(push(client)) == [("app.example.com", "update")]
    edit = _saves(sim)[-1]
    assert edit["isEdit"] == "true" and edit["configFile"] == "_shared.yml"
    assert sim.copies_of(REF) == ["_shared.yml"]


def test_changing_only_the_wanted_file_is_not_a_pending_push(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("_shared.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    host = _create(client, service, route_options={"config_file": "_shared.yml"})
    push(client)
    n = len(_saves(sim))
    r = client.put(
        f"/api/proxy-hosts/{host['id']}",
        json=_body(service, entry_points=["websecure"], route_options={"config_file": "thing.yml"}),
    )
    assert r.json()["sync_state"] == "in_sync"  # not part of the route's content
    assert push(client)["results"] == [] and len(_saves(sim)) == n


def test_config_file_is_not_part_of_the_content_signature() -> None:
    a = ProxyHost(hostname="a.example.com", upstream="http://x:1", route_options={})
    b = ProxyHost(
        hostname="a.example.com", upstream="http://x:1", route_options={"config_file": "x.yml"}
    )
    assert desired_signature(a, "s") == desired_signature(b, "s")


# ---------------------------------------------------------------------------
# Where a route lives now, and the opt-in move
# ---------------------------------------------------------------------------


def _shared_host(client: TestClient, service: Service, sim: SimTM, **over: Any) -> dict[str, Any]:
    """A host created into ``_shared.yml``, whose wanted file is then changed to
    the template (``thing.yml``): it sits in the "wrong" file."""
    host = _create(client, service, route_options={"config_file": "_shared.yml", **over})
    assert failures(push(client)) == []
    assert sim.file_of(REF) == "_shared.yml"
    r = client.put(
        f"/api/proxy-hosts/{host['id']}",
        json=_body(service, entry_points=["websecure"], route_options={**over}),
    )
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def test_a_host_in_shared_is_flagged_informationally_and_nothing_moves_on_its_own(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("_shared.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    host = _shared_host(client, service, sim)
    assert host["tm_config_file"] == "_shared.yml"
    assert host["config_file_resolved"] == "thing.yml"
    assert host["config_file_misplaced"] is True and host["config_file_movable"] is True
    assert "_shared.yml" in host["config_file_note"] and "thing.yml" in host["config_file_note"]
    assert host["sync_state"] == "in_sync"  # informational, never an error
    listed = client.get("/api/proxy-hosts").json()[0]
    assert listed["config_file_misplaced"] is True
    n = len(_saves(sim))
    assert push(client)["results"] == [] and len(_saves(sim)) == n  # no automatic move


def test_move_resaves_into_the_resolved_file_with_the_move_request_shape_and_verifies(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("_shared.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    host = _shared_host(client, service, sim)

    r = client.post(f"/api/proxy-hosts/{host['id']}/move-config-file")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["move"]["ok"] is True and "thing.yml" in body["move"]["message"]
    assert body["tm_config_file"] == "thing.yml" and body["config_file_misplaced"] is False

    move = _saves(sim)[-1]
    assert move["isEdit"] == "true"
    assert move["originalId"] == f"_shared.yml::{REF}"
    assert move["configFile"] == "thing.yml"
    assert move["serviceName"] == REF
    assert move["httpRule"] == "Host(`app.example.com`)" and move["entryPoints"] == "websecure"
    assert sim.copies_of(REF) == ["thing.yml"]  # one copy, in the new file


def test_move_takes_the_redirect_companion_along_first(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("_shared.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    host = _create(
        client,
        service,
        entry_points=["web", "websecure"],
        route_options={"config_file": "_shared.yml", "http_redirect": True},
    )
    push(client)
    client.put(
        f"/api/proxy-hosts/{host['id']}",
        json=_body(
            service, entry_points=["web", "websecure"], route_options={"http_redirect": True}
        ),
    )
    n = len(_saves(sim))
    r = client.post(f"/api/proxy-hosts/{host['id']}/move-config-file")
    assert r.status_code == 200, r.text
    companion, main = _saves(sim)[n:]
    assert companion["serviceName"] == f"{REF}-redirect"
    assert companion["originalId"] == f"_shared.yml::{REF}-redirect"
    assert companion["configFile"] == "thing.yml" and companion["serviceRef"] == f"{REF}-service"
    assert main["serviceName"] == REF and main["originalId"] == f"_shared.yml::{REF}"
    assert sim.copies_of(REF) == ["thing.yml"]
    assert sim.copies_of(f"{REF}-redirect") == ["thing.yml"]


def test_move_that_leaves_a_copy_behind_is_reported_not_trusted(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("_shared.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    host = _shared_host(client, service, sim)
    sim.leak_on_move = True
    r = client.post(f"/api/proxy-hosts/{host['id']}/move-config-file")
    assert r.status_code == 409, r.text
    assert "accepted the move but" in r.json()["detail"]["message"]


def test_move_refuses_an_unwritable_target_a_disabled_host_and_a_route_that_is_not_ours(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("dynamic.yml", "apps.yml"))
    client = _client(monkeypatch, sim, TRAEFIK_MANAGER_CONFIG_FILE="")
    host = _create(client, service, route_options={"config_file": "apps.yml"})
    push(client)
    client.put(
        f"/api/proxy-hosts/{host['id']}",
        json=_body(
            service, entry_points=["websecure"], route_options={"config_file": "dynamic.yml"}
        ),
    )
    # Now ask for a file TM cannot write to: refused with the file list.
    with Session(get_engine()) as s:
        ph = s.get(ProxyHost, host["id"])
        assert ph is not None
        ph.route_options = {"config_file": "ghost.yml"}
        s.add(ph)
        s.commit()
    r = client.post(f"/api/proxy-hosts/{host['id']}/move-config-file")
    assert r.status_code == 409 and "CONFIG_PATHS" in r.json()["detail"]["message"]

    # disabled here
    client.post(f"/api/proxy-hosts/{host['id']}/enabled", json={"enabled": False})
    r = client.post(f"/api/proxy-hosts/{host['id']}/move-config-file")
    assert r.status_code == 409 and "disabled" in r.json()["detail"]["message"]

    # adopted (hand-written) route: never moved
    sim.add("apps.yml", "hand-written", "adopted.example.com")
    adopted = client.post(
        "/api/proxy-hosts",
        json={
            "hostname": "adopted.example.com",
            "upstream": "http://10.0.0.9:8080",
            "service_id": service.id,
            "adopt": True,
            "route_options": {"config_file": "dynamic.yml"},
        },
    )
    assert adopted.status_code == 201, adopted.text
    body = adopted.json()
    assert body["tm_config_file"] == "apps.yml" and body["config_file_misplaced"] is True
    assert body["config_file_movable"] is False
    assert "never moved automatically" in body["config_file_note"]
    r = client.post(f"/api/proxy-hosts/{adopted.json()['id']}/move-config-file")
    assert r.status_code == 409 and "adopted" in r.json()["detail"]["message"]
    assert sim.file_of("hand-written") == "apps.yml"


def test_move_when_already_in_the_wanted_file_is_a_no_op(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("_shared.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    host = _create(client, service)
    push(client)
    n = len(_saves(sim))
    r = client.post(f"/api/proxy-hosts/{host['id']}/move-config-file")
    assert r.status_code == 200 and "nothing to move" in r.json()["move"]["message"]
    assert len(_saves(sim)) == n


def test_push_can_move_hosts_as_an_explicit_opt_in(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("_shared.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    _shared_host(client, service, sim)

    plan = push(client, move_config_files=["app.example.com"], dry_run=True)
    assert [(i["action"], i["planned"]) for i in plan["results"]] == [("move", True)]
    assert sim.file_of(REF) == "_shared.yml"

    result = push(client, move_config_files=["app.example.com"])
    assert actions(result) == [("app.example.com", "move")]
    assert sim.file_of(REF) == "thing.yml"
    missing = push(client, move_config_files=["nobody.example.com"])
    assert failures(missing)[0]["message"] == "no such proxy host"


def test_the_dry_run_adapter_exercises_create_and_move(
    dry_client: TestClient, service: Service
) -> None:
    host = _create(dry_client, service, route_options={"config_file": "_shared.yml"})
    assert failures(push(dry_client)) == []
    dry_client.put(
        f"/api/proxy-hosts/{host['id']}",
        json=_body(service, entry_points=["websecure"], route_options={}),
    )
    got = _get(dry_client, host["id"])
    assert got["tm_config_file"] == "_shared.yml" and got["config_file_movable"] is True
    r = dry_client.post(f"/api/proxy-hosts/{host['id']}/move-config-file")
    assert r.status_code == 200, r.text
    assert _get(dry_client, host["id"])["tm_config_file"] == "thing.yml"


# ---------------------------------------------------------------------------
# Catalog + adapter reads
# ---------------------------------------------------------------------------


def test_catalog_lists_the_known_files_and_the_resolved_default(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("_shared.yml", "media.yml"), config_dir=True)
    client = _client(monkeypatch, sim)
    body = client.get(
        "/api/proxy/entrypoints", params={"service": "thing", "hostname": "App.example.com"}
    ).json()
    assert body["config_files"] == ["_shared.yml", "media.yml"]
    assert body["config_files_known"] is True and body["config_files_can_create"] is True
    assert body["config_file_template"] == "{service}.yml"
    assert body["default_config_file"] == "thing.yml"
    assert body["config_file_default"] == "_shared.yml"  # the manager's own default file


def test_catalog_falls_back_to_the_files_routes_were_seen_in(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("dynamic.yml", "apps.yml"), configs_status=404)  # no /api/configs
    sim.add("apps.yml", "x", "x.example.com")
    client = _client(monkeypatch, sim)
    body = client.get("/api/proxy/entrypoints").json()
    assert body["config_files"] == ["apps.yml"]
    assert body["config_files_can_create"] is None  # cannot tell


def test_list_config_files_reads_api_configs(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    sim = SimTM(files=("a.yml", "b.yml"), config_dir=True)
    _patch(monkeypatch, sim)
    files = asyncio.run(TraefikManagerProxyAdapter(_settings()).list_config_files())
    assert files == ConfigFiles(files=("a.yml", "b.yml"), can_create=True, default="a.yml")
    assert (
        asyncio.run(
            TraefikManagerProxyAdapter(_settings(traefik_manager_url="")).list_config_files()
        )
        is None
    )


def test_the_dry_run_adapter_lists_config_files() -> None:
    import asyncio

    files = asyncio.run(DryRunProxyAdapter().list_config_files())
    assert files is not None and "_shared.yml" in files.files


# ---------------------------------------------------------------------------
# route_options.config_file: validation and YAML
# ---------------------------------------------------------------------------


def test_route_options_keep_all_defaults_empty_and_config_file_alone_is_not_a_default() -> None:
    assert RouteOptions().to_dict()["config_file"] == ""
    assert ProxyHostSpec(hostname="a.example.com", route_options={}).route_options == {}
    assert (
        ProxyHostSpec(hostname="a.example.com", route_options={"config_file": ""}).route_options
        == {}
    )
    spec = ProxyHostSpec(hostname="a.example.com", route_options={"config_file": "media"})
    assert spec.route_options["config_file"] == "media.yml"
    with pytest.raises(ValidationError):
        ProxyHostSpec(hostname="a.example.com", route_options={"config_file": "../x"})


def test_config_file_round_trips_through_save_to_yaml(
    service: Service, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sim = SimTM(files=("_shared.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    _create(client, service, route_options={"config_file": "special.yml"})
    _create(client, service, hostname="plain.example.com")
    assert client.post("/api/registry/save").status_code == 200
    doc = yaml.safe_load((tmp_path / "services.yaml").read_text(encoding="utf-8"))
    by_host = {h["hostname"]: h for h in doc["services"][0]["proxy_hosts"]}
    assert by_host["app.example.com"]["route_options"]["config_file"] == "special.yml"
    assert by_host["plain.example.com"]["route_options"] == {}  # all defaults stay {}
    parsed, _ = read_yaml_file(tmp_path / "services.yaml")
    assert parsed.services[0].proxy_hosts[0].route_options.get("config_file") in (
        "special.yml",
        None,
    )
    assert client.get("/api/registry/status").json()["dirty"] is False


def test_the_form_and_list_carry_the_config_file_ui(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = SimTM(files=("_shared.yml",), config_dir=True)
    client = _client(monkeypatch, sim)
    html = client.get("/proxy-hosts/new").text
    assert 'id="ph-config-file"' in html and 'list="ph-config-files"' in html
    assert "config_file: document.getElementById" in html
    assert "(from TRAEFIK_MANAGER_CONFIG_FILE)" in html
    listing = client.get("/proxy-hosts").text
    assert "host-file" in listing and "move-config-file" in listing


def test_env_layout_documents_the_setting() -> None:
    from orchestrator.setup.envfile import render_example

    text = render_example()
    assert "TRAEFIK_MANAGER_CONFIG_FILE={service}.yml" in text


def test_cli_push_moves_only_the_hosts_it_is_asked_to(
    session: Session, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from orchestrator.cli import cli
    from tests.test_proxy_push import _patch_cli_adapter

    adapter = DryRunProxyAdapter()
    _patch_cli_adapter(monkeypatch, adapter)
    session.add(
        ProxyHost(
            hostname="cli.example.com",
            upstream="http://10.0.0.4:80",
            service_id=service.id,
            entry_points=["websecure"],
            route_options={"config_file": "_shared.yml"},
            extra={"traefik_manager_route_id": "dry-run:cli.example.com"},
        )
    )
    session.commit()
    assert CliRunner().invoke(cli, ["proxy", "push", "--yes"]).exit_code == 0
    assert adapter.route_files["dry-run:cli.example.com"] == "_shared.yml"

    ph = session.exec(select(ProxyHost)).one()
    ph.route_options = {}  # wanted file is now the template: thing.yml
    session.add(ph)
    session.commit()

    plain = CliRunner().invoke(cli, ["proxy", "push", "--yes"])
    assert plain.exit_code == 0 and adapter.moves == []  # a normal push never moves

    moved = CliRunner().invoke(
        cli, ["proxy", "push", "--yes", "--move-config-file", "cli.example.com"]
    )
    assert moved.exit_code == 0, moved.output
    assert adapter.moves == [("dry-run:cli.example.com", "thing.yml")]
    assert "moved from _shared.yml to thing.yml" in moved.output
