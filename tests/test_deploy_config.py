"""A service's ``deploy:`` block: validation, the registry file, the API and the CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.cli import cli
from orchestrator.db import session as session_module
from orchestrator.db.models import Service
from orchestrator.db.session import get_engine
from orchestrator.deploy.policy import check_target, normalize_deploy, preset
from orchestrator.registry.loader import read_yaml_file, reconcile_yaml_into_db
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin

# -- policy ---------------------------------------------------------------------------------


def test_nothing_configured_stays_nothing() -> None:
    assert normalize_deploy(None) == {}
    assert normalize_deploy({}) == {}


def test_defaults_are_filled_in() -> None:
    cfg = normalize_deploy({"on_failure": "stop"})
    assert cfg["enabled"] is True and cfg["on_failure"] == "stop"
    assert cfg["guard"]["post"] == {"probes": True, "timeout_s": 180, "settle_s": 30}
    assert cfg["guard"]["pre"] == {"probes": False, "backup": None}
    assert cfg["trigger"]["webhook"] is None and cfg["pull_image"] is True


def test_the_webhook_and_backup_guard_are_normalised() -> None:
    cfg = normalize_deploy(
        {
            "trigger": {"webhook": {"provider": "GitLab", "branch": " dev "}},
            "guard": {"pre": {"probes": True, "backup": {"max_age_h": "12"}}},
        }
    )
    assert cfg["trigger"]["webhook"] == {"provider": "gitlab", "branch": "dev"}
    assert cfg["guard"]["pre"]["backup"] == {"max_age_h": 12, "take_if_older": True}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"bogus": 1}, "unknown deploy key"),
        ({"on_failure": "explode"}, "on_failure must be one of"),
        ({"trigger": {"webhook": {"provider": "svn"}}}, "provider must be one of"),
        ({"trigger": {"cron": "daily"}}, "unknown deploy trigger"),
        ({"guard": {"pre": {"backup": {"max_age_h": 0}}}}, "at least 1"),
        ({"guard": {"post": {"timeout_s": "soon"}}}, "whole number"),
        ({"guard": {"pre": {"probes": "yes"}}}, "true or false"),
        ({"prune": 1}, "true or false"),
        ("text", "must be an object"),
    ],
)
def test_bad_blocks_say_what_is_wrong(raw: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_deploy(raw)


def test_a_deploy_needs_a_portainer_stack() -> None:
    cfg = normalize_deploy({"on_failure": "stop"})
    check_target(cfg, {"type": "portainer", "endpoint": 1, "stack": "x"})
    check_target({}, {})  # nothing to check
    for controller in ({}, {"type": "portainer", "endpoint": 1, "container": "c"}):
        with pytest.raises(ValueError, match="Portainer stack"):
            check_target(cfg, controller)


def test_presets() -> None:
    assert preset("none")["guard"]["post"]["probes"] is False
    assert preset("probes+backup")["guard"]["pre"]["backup"]["max_age_h"] == 24
    with pytest.raises(ValueError, match="unknown preset"):
        preset("yolo")


# -- registry file, API, CLI -----------------------------------------------------------------

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
  - slug: dev-xymbols
    name: dev.xymbols
    parent: portainer-vm
    controller: {type: portainer, endpoint: 1, stack: dev-xymbols}
  - slug: plain
    name: plain
    parent: portainer-vm
"""


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY)
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()
    return path


@pytest.fixture
def client(registry: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_the_file_round_trips_through_the_database(registry: Path, client: TestClient) -> None:
    r = client.put(
        "/api/registry/services/dev-xymbols",
        json={"deploy": {"on_failure": "stop", "guard": {"pre": {"probes": True}}}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["deploy"]["on_failure"] == "stop"
    assert client.post("/api/registry/save").status_code == 200
    parsed, _ = read_yaml_file(registry)
    saved = next(s for s in parsed.services if s.slug == "dev-xymbols")
    assert saved.deploy["on_failure"] == "stop"
    plain = next(s for s in parsed.services if s.slug == "plain")
    assert plain.deploy == {}
    assert "deploy" not in yaml.safe_load(registry.read_text())["services"][2]  # empty = absent
    assert client.post("/api/registry/reset").status_code == 200
    with Session(get_engine()) as s:
        row = s.exec(select(Service).where(Service.slug == "dev-xymbols")).one()
        assert row.deploy["guard"]["pre"]["probes"] is True


def test_the_api_refuses_a_deploy_without_a_stack(client: TestClient) -> None:
    r = client.put("/api/registry/services/plain", json={"deploy": {"on_failure": "stop"}})
    assert r.status_code == 400 and "Portainer stack" in r.text
    r = client.put("/api/registry/services/dev-xymbols", json={"deploy": {"on_failure": "x"}})
    assert r.status_code == 400 and "on_failure" in r.text


def test_dropping_the_stack_link_while_a_deploy_exists_is_refused(client: TestClient) -> None:
    ok = client.put("/api/registry/services/dev-xymbols", json={"deploy": {"prune": True}})
    assert ok.status_code == 200
    r = client.put("/api/registry/services/dev-xymbols", json={"controller": {}})
    assert r.status_code == 400 and "Portainer stack" in r.text


def test_the_file_is_checked_when_loaded(registry: Path) -> None:
    registry.write_text(
        REGISTRY.replace(
            "  - slug: plain\n    name: plain\n    parent: portainer-vm\n",
            "  - slug: plain\n    name: plain\n    parent: portainer-vm\n"
            "    deploy: {on_failure: stop}\n",
        )
    )
    from orchestrator.registry.loader import validate_references

    parsed, _ = read_yaml_file(registry)
    with pytest.raises(Exception, match=r"plain.*Portainer stack"):
        validate_references(parsed)


def test_cli_set_show_and_clear(registry: Path) -> None:
    run = CliRunner()
    out = run.invoke(
        cli,
        [
            "deploy", "set", "dev-xymbols", "--preset", "probes+backup", "--on-failure", "stop",
            "--backup-max-age", "6", "--webhook", "gitlab", "--branch", "dev", "--no-prune",
        ],
    )  # fmt: skip
    assert out.exit_code == 0, out.output
    saved = yaml.safe_load(registry.read_text())["services"][1]["deploy"]
    assert saved["guard"]["pre"]["backup"] == {"max_age_h": 6, "take_if_older": True}
    assert saved["trigger"]["webhook"] == {"provider": "gitlab", "branch": "dev"}
    assert saved["on_failure"] == "stop"

    shown = run.invoke(cli, ["deploy", "show", "dev-xymbols"])
    assert shown.exit_code == 0
    assert "backup newer than 6h" in shown.output and "webhook (gitlab, branch dev)" in shown.output

    # only the options passed change
    again = run.invoke(cli, ["deploy", "set", "dev-xymbols", "--no-backup-guard"])
    assert again.exit_code == 0, again.output
    saved = yaml.safe_load(registry.read_text())["services"][1]["deploy"]
    assert saved["guard"]["pre"]["backup"] is None and saved["on_failure"] == "stop"

    gone = run.invoke(cli, ["deploy", "clear", "dev-xymbols"])
    assert gone.exit_code == 0
    assert "deploy" not in yaml.safe_load(registry.read_text())["services"][1]


def test_cli_refuses_what_cannot_work(registry: Path) -> None:
    run = CliRunner()
    no_stack = run.invoke(cli, ["deploy", "set", "plain", "--on-failure", "stop"])
    assert no_stack.exit_code != 0 and "Portainer stack" in no_stack.output
    bad = run.invoke(cli, ["deploy", "set", "dev-xymbols", "--branch", "dev"])
    assert bad.exit_code != 0 and "needs a webhook" in bad.output
    ghost = run.invoke(cli, ["deploy", "show", "ghost"])
    assert ghost.exit_code != 0
