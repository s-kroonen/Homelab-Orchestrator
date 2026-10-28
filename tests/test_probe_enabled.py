"""``Probe.enabled``: a switched-off probe is kept but never run and never counted.

The motivating case is the auto-created ``traefik:<hostname>`` probe of a proxy
host that is disabled (its route is not served, so the probe would fail for no
reason - and it may be ``required``). Its ``required`` / timeout settings must
survive disable -> enable, so it is switched off, not deleted.
"""

# ruff: noqa: F811 - imported pytest fixtures are re-declared as test arguments
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.cli import cli
from orchestrator.db.models import HealthScanSample, Probe, ProxyHost, Service, ServiceState
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import HealthState, ProbeKind
from orchestrator.domain.schemas import ProbeResult
from orchestrator.health.engine import HealthEngine
from orchestrator.registry.editor import RegistryEditor
from orchestrator.registry.loader import (
    read_yaml_file,
    reconcile_yaml_into_db,
    save_registry_to_yaml,
)
from orchestrator.scheduler.health_scanner import HealthScanner
from orchestrator.web.routers.ui import _probe_row
from tests.test_proxy_hosts import (  # noqa: F401 - app/client are fixtures
    _enable_proxy_base_url,
    _patch_traefik_transport,
    _probes_for,
    _traefik_responder,
    app,
    client,
)

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class RecordingEngine(HealthEngine):
    """Runs no real probe: records which ones the scan asked to run and answers
    from ``states`` (default HEALTHY)."""

    def __init__(self, states: dict[str, HealthState] | None = None) -> None:
        super().__init__()
        self.ran: list[str] = []
        self.states = states or {}

    async def run_probe(self, row: Probe) -> ProbeResult:
        self.ran.append(row.name)
        return ProbeResult(
            probe_name=row.name,
            kind=row.kind,
            state=self.states.get(row.name, HealthState.HEALTHY),
            message="from the test",
        )


def _add_probe(
    session: Session, service: Service, name: str, *, required: bool = True, enabled: bool = True
) -> Probe:
    probe = Probe(
        service_id=service.id,  # type: ignore[arg-type]
        name=name,
        kind=ProbeKind.HTTP,
        required=required,
        enabled=enabled,
        config={"url": "http://x/"},
    )
    session.add(probe)
    session.commit()
    session.refresh(probe)
    return probe


async def test_a_disabled_required_probe_is_skipped_and_does_not_count(
    session: Session, service: Service
) -> None:
    _add_probe(session, service, "ok")
    _add_probe(session, service, "off", enabled=False)  # required, and would FAIL
    engine = RecordingEngine({"off": HealthState.FAILED})

    verdict = await engine.scan(session, service)

    assert engine.ran == ["ok"]  # never run
    assert verdict.state is HealthState.HEALTHY
    assert verdict.reason == "all 1 required probe(s) passed"
    skipped = next(r for r in verdict.probe_results if r.probe_name == "off")
    assert skipped.details == {"skipped": True, "skipped_because": "disabled"}
    assert skipped.message == "skipped — disabled"
    # ...and the persisted state (dashboard badge, backup gate) agrees.
    state = session.get(ServiceState, service.id)
    assert state is not None and state.last_verdict is HealthState.HEALTHY
    assert any(
        r["probe_name"] == "off" and r["details"]["skipped_because"] == "disabled"
        for r in state.last_probe_results
    )


async def test_the_dashboard_shows_a_disabled_probe_as_skipped(
    session: Session, service: Service
) -> None:
    _add_probe(session, service, "off", enabled=False)
    verdict = await RecordingEngine().scan(session, service)
    row = _probe_row(verdict.probe_results[0].model_dump(mode="json"))
    assert row["state"] == "skipped" and row["css"] == "skipped"
    assert row["message"] == "skipped — disabled"


async def test_all_probes_disabled_falls_back_to_the_no_required_probe_rule(
    session: Session, service: Service
) -> None:
    _add_probe(session, service, "a", enabled=False)
    _add_probe(session, service, "b", required=False, enabled=False)
    engine = RecordingEngine()

    verdict = await engine.scan(session, service)

    assert engine.ran == []
    assert verdict.state is HealthState.UNKNOWN
    assert verdict.reason.startswith("no required probes are configured")
    assert len(verdict.probe_results) == 2  # still recorded, all skipped
    assert all(r.details["skipped"] for r in verdict.probe_results)


def test_aggregate_ignores_skipped_results_even_for_a_required_probe(
    session: Session, service: Service
) -> None:
    off = Probe(service_id=1, name="off", kind=ProbeKind.HTTP, required=True, enabled=False)
    results = [
        ProbeResult(
            probe_name="off",
            kind=ProbeKind.HTTP,
            state=HealthState.UNKNOWN,
            details={"skipped": True, "skipped_because": "disabled"},
        )
    ]
    verdict = HealthEngine().aggregate([off], results)
    assert verdict.state is HealthState.UNKNOWN and "no required probes" in verdict.reason


async def test_a_disabled_probe_does_not_reach_the_health_history(
    session: Session, service: Service
) -> None:
    _add_probe(session, service, "ok")
    _add_probe(session, service, "off", enabled=False)
    scanner = HealthScanner(settings=config_module.get_settings())
    scanner._engine = RecordingEngine({"off": HealthState.FAILED})

    await scanner.poll_once()

    samples = session.exec(select(HealthScanSample)).all()
    assert [s.state for s in samples] == [HealthState.HEALTHY]


async def test_probe_test_still_runs_a_disabled_probe_on_demand(
    session: Session, service: Service
) -> None:
    """run_probe is the debugging primitive ("Test" button): it ignores ``enabled``."""
    probe = _add_probe(session, service, "off", enabled=False)
    engine = RecordingEngine()
    result = await engine.run_probe(probe)
    assert engine.ran == ["off"] and result.state is HealthState.HEALTHY


# ---------------------------------------------------------------------------
# A disabled proxy host switches its traefik: probe off and back on
# ---------------------------------------------------------------------------


def _traefik_probe(service: Service) -> Probe:
    (probe,) = _probes_for(service.id)  # type: ignore[arg-type]
    return probe


def _create_host(client: TestClient, service: Service) -> dict[str, Any]:
    r = client.post(
        "/api/proxy-hosts", json={"hostname": "media.example.com", "service_id": service.id}
    )
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def _tune_probe(client: TestClient, probe: Probe) -> None:
    r = client.put(f"/api/registry/probes/{probe.id}", json={"required": True, "timeout_s": 33})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("bulk", [False, True])
async def test_disabling_a_host_skips_its_probe_and_enabling_brings_it_back_unchanged(
    client: TestClient,
    service: Service,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    bulk: bool,
) -> None:
    _enable_proxy_base_url(monkeypatch)
    host = _create_host(client, service)
    probe = _traefik_probe(service)
    assert probe.enabled is True and probe.required is False  # the auto-created defaults
    _tune_probe(client, probe)  # the operator: this route gates the service, 33 s

    def switch(on: bool) -> None:
        if bulk:
            r = client.post(
                "/api/bulk/proxy-hosts",
                json={"ids": [host["id"]], "action": "enable" if on else "disable"},
            )
        else:
            r = client.post(f"/api/proxy-hosts/{host['id']}/enabled", json={"enabled": on})
        assert r.status_code == 200, r.text

    # The host is down: its (now required) probe would FAIL - but it is switched off.
    switch(False)
    off = _traefik_probe(service)
    assert (off.enabled, off.required, off.timeout_s) == (False, True, 33)
    session.expire_all()
    engine = RecordingEngine({off.name: HealthState.FAILED})
    verdict = await engine.scan(session, service)
    assert engine.ran == [] and verdict.state is HealthState.UNKNOWN  # not FAILED

    # Back on: it runs again, with exactly the settings the operator gave it.
    switch(True)
    on = _traefik_probe(service)
    assert (on.enabled, on.required, on.timeout_s) == (True, True, 33)
    session.expire_all()
    engine = RecordingEngine({on.name: HealthState.FAILED})
    verdict = await engine.scan(session, service)
    assert engine.ran == [on.name] and verdict.state is HealthState.FAILED


def test_the_probe_sync_is_audited_and_keeps_one_probe(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_proxy_base_url(monkeypatch)
    host = _create_host(client, service)
    client.post(f"/api/proxy-hosts/{host['id']}/enabled", json={"enabled": False})
    client.post(f"/api/proxy-hosts/{host['id']}/enabled", json={"enabled": True})
    assert len(_probes_for(service.id)) == 1  # type: ignore[arg-type]
    from tests.test_proxy_hosts import _audit_actions_all

    assert _audit_actions_all().count("probe.updated") == 2


def test_resync_follows_the_hosts_enabled_state(
    client: TestClient, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host switched off whose probe was left on (older data) is corrected by the
    next Resync backfill."""
    _enable_proxy_base_url(monkeypatch)
    host = _create_host(client, service)
    with Session(get_engine()) as s:
        ph = s.get(ProxyHost, host["id"])
        assert ph is not None
        ph.enabled = False  # as an older version / a raw edit left it
        s.add(ph)
        s.commit()
    assert _traefik_probe(service).enabled is True
    monkeypatch.setenv("TRAEFIK_API_URL", "http://traefik.test:8080")
    config_module.reset_settings_cache()
    _patch_traefik_transport(monkeypatch, _traefik_responder(routers=[], services=[]))
    r = client.post("/api/proxy-hosts/resync")
    assert r.status_code == 200, r.text
    assert _traefik_probe(service).enabled is False


# ---------------------------------------------------------------------------
# API: manual enable/disable for any probe, list shows it, bulk required unaffected
# ---------------------------------------------------------------------------


def test_probe_api_toggles_enabled_and_lists_it(client: TestClient, service: Service) -> None:
    r = client.post(
        f"/api/registry/services/{service.slug}/probes",
        json={"name": "web", "kind": "http", "config": {"url": "http://x/"}},
    )
    assert r.status_code == 201, r.text
    probe = r.json()
    assert probe["enabled"] is True

    r = client.put(f"/api/registry/probes/{probe['id']}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    listed = client.get(f"/api/registry/services/{service.slug}/probes").json()
    assert [(p["name"], p["enabled"], p["required"]) for p in listed] == [("web", False, True)]

    # "Probes required/optional" bulk is about `required`, not `enabled`.
    r = client.post("/api/bulk/services", json={"ids": [service.slug], "action": "probes_optional"})
    assert r.status_code == 200, r.text
    (p,) = client.get(f"/api/registry/services/{service.slug}/probes").json()
    assert (p["required"], p["enabled"]) == (False, False)

    assert (
        client.put(f"/api/registry/probes/{probe['id']}", json={"enabled": None}).status_code == 400
    )
    r = client.put(f"/api/registry/probes/{probe['id']}", json={"enabled": True})
    assert r.json()["enabled"] is True and r.json()["required"] is False


def test_a_probe_can_be_created_switched_off(client: TestClient, service: Service) -> None:
    r = client.post(
        f"/api/registry/services/{service.slug}/probes",
        json={"name": "later", "kind": "http", "enabled": False, "config": {"url": "http://x/"}},
    )
    assert r.status_code == 201 and r.json()["enabled"] is False


def test_the_service_page_offers_the_toggle_and_a_disabled_badge(
    client: TestClient, service: Service
) -> None:
    assert client.get(f"/services/{service.slug}").status_code == 200
    js = client.get("/static/service.js").text
    assert 'key: "enabled", label: "Run it", type: "checkbox"' in js  # in the probe drawer
    assert '"Stop running it"' in js and '"Run it again"' in js  # in the row menu
    assert 'title: "Not run and never counted"' in js  # the "off" badge


# ---------------------------------------------------------------------------
# YAML: schema / loader / editor round trip, omitted when true
# ---------------------------------------------------------------------------

REGISTRY = """\
version: 1
nodes:
  - name: eve1
    always_on: true
    power_mgr_target: eve1
    notes: ""
backup_policies: []
services:
  - slug: media
    name: Media
    node: eve1
    guest_kind: vm
    guest_id: 101
    probes:
      - name: web
        kind: http
        config: {url: "http://10.0.0.5/"}
      - name: parked
        kind: http
        enabled: false
        config: {url: "http://10.0.0.5/other"}
    proxy_hosts: []
"""


def test_yaml_round_trip_keeps_enabled_and_omits_it_when_true(
    session: Session, tmp_path: Path
) -> None:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY, encoding="utf-8")
    parsed, _ = read_yaml_file(path)
    assert [(p.name, p.enabled) for p in parsed.services[0].probes] == [
        ("web", True),
        ("parked", False),
    ]

    reconcile_yaml_into_db(session, path)
    session.commit()
    rows = {p.name: p.enabled for p in session.exec(select(Probe)).all()}
    assert rows == {"web": True, "parked": False}

    out = tmp_path / "saved.yaml"
    save_registry_to_yaml(session, out, actor="test")
    doc = yaml.safe_load(out.read_text(encoding="utf-8"))
    probes = {p["name"]: p for p in doc["services"][0]["probes"]}
    assert "enabled" not in probes["web"]  # true is the default: omitted
    assert probes["parked"]["enabled"] is False


def test_a_saved_registry_with_a_disabled_host_loads_its_probe_switched_off(
    session: Session, tmp_path: Path
) -> None:
    """Registries saved before probes had ``enabled`` list a disabled host's
    ``traefik:`` probe as on: the host is not served, so the probe follows it."""
    path = tmp_path / "services.yaml"
    path.write_text(
        REGISTRY.replace(
            "    proxy_hosts: []",
            "    proxy_hosts:\n      - hostname: media.example.com\n        enabled: false",
        ).replace("name: web", "name: traefik:media.example.com"),
        encoding="utf-8",
    )
    reconcile_yaml_into_db(session, path)
    session.commit()
    rows = {p.name: p.enabled for p in session.exec(select(Probe)).all()}
    assert rows["traefik:media.example.com"] is False


def test_editor_sets_and_clears_enabled(tmp_path: Path) -> None:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY, encoding="utf-8")
    editor = RegistryEditor(path)
    assert editor.set_probe_enabled("media", "web", False) is True
    assert editor.set_probe_enabled("media", "parked", True) is True
    assert editor.set_probe_enabled("media", "nope", True) is False
    editor.save()
    text = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    by_name = {p["name"]: p for p in doc["services"][0]["probes"]}
    assert by_name["web"]["enabled"] is False
    assert "enabled" not in by_name["parked"]  # true: key removed, not written


def test_cli_probe_disable_enable_and_list(tmp_path: Path) -> None:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY, encoding="utf-8")
    runner = CliRunner()

    listed = runner.invoke(cli, ["probe", "list", "media", "--file", str(path)])
    assert listed.exit_code == 0, listed.output
    assert "1 gating, 1 disabled" in listed.output
    assert "off  parked" in listed.output and "REQ  web" in listed.output

    r = runner.invoke(cli, ["probe", "disable", "media", "web", "--file", str(path)])
    assert r.exit_code == 0, r.output
    listed = runner.invoke(cli, ["probe", "list", "media", "--file", str(path)])
    assert "0 gating, 2 disabled" in listed.output

    r = runner.invoke(cli, ["probe", "enable", "media", "parked", "--file", str(path)])
    assert r.exit_code == 0, r.output
    assert (
        runner.invoke(cli, ["probe", "enable", "media", "nope", "--file", str(path)]).exit_code != 0
    )


# ---------------------------------------------------------------------------
# Migration 0016
# ---------------------------------------------------------------------------


def _insert(conn: sa.Connection, table: str, **values: Any) -> int:
    """Insert a row, filling every other NOT NULL column that has no default."""
    cols = {c["name"]: c for c in sa.inspect(conn).get_columns(table)}
    row = dict(values)
    for name, col in cols.items():
        if name in row or col.get("default") is not None or col["primary_key"]:
            continue
        if col["nullable"]:
            continue
        kind = str(col["type"]).upper()
        row[name] = "{}" if "JSON" in kind else 0 if ("INT" in kind or "BOOL" in kind) else ""
    names = ", ".join(f'"{k}"' for k in row)
    marks = ", ".join(f":{k}" for k in row)
    result = conn.execute(sa.text(f"INSERT INTO {table} ({names}) VALUES ({marks})"), row)
    return int(result.lastrowid)


def test_migration_0016_adds_enabled_defaulting_true_and_parks_probes_of_disabled_hosts() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    url = config_module.get_settings().database_url
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0015_proxy_host_pushed_sig")
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        node = _insert(conn, "node", name="n1", always_on=1)
        svc = _insert(conn, "service", slug="s", name="S", node_id=node, guest_id=1)
        other = _insert(conn, "service", slug="t", name="T", node_id=node, guest_id=2)
        for sid, name in (
            (svc, "traefik:off.example.com"),
            (svc, "traefik:on.example.com"),
            (svc, "manual"),
            (other, "traefik:off.example.com"),
        ):
            _insert(
                conn,
                "probe",
                service_id=sid,
                name=name,
                kind="HTTP",
                required=1,
                timeout_s=5,
                order=0,
                config="{}",
            )
        _insert(
            conn,
            "proxy_host",
            hostname="off.example.com",
            service_id=svc,
            enabled=0,
            upstream="",
            router_provider="TRAEFIK",
            extra="{}",
            wake_on_hit=0,
            entry_points="[]",
            cert_resolver="",
            route_options="{}",
        )
        _insert(
            conn,
            "proxy_host",
            hostname="on.example.com",
            service_id=svc,
            enabled=1,
            upstream="",
            router_provider="TRAEFIK",
            extra="{}",
            wake_on_hit=0,
            entry_points="[]",
            cert_resolver="",
            route_options="{}",
        )

    command.upgrade(cfg, "0016_probe_enabled")

    with engine.connect() as conn:
        assert "enabled" in {c["name"] for c in sa.inspect(conn).get_columns("probe")}
        rows = {
            (r.service_id, r.name): bool(r.enabled)
            for r in conn.execute(sa.text("SELECT service_id, name, enabled FROM probe"))
        }
    assert rows == {
        (svc, "traefik:off.example.com"): False,  # its host is disabled
        (svc, "traefik:on.example.com"): True,
        (svc, "manual"): True,  # untouched
        (other, "traefik:off.example.com"): True,  # another service's probe
    }

    command.downgrade(cfg, "0015_proxy_host_pushed_sig")
    with engine.connect() as conn:
        assert "enabled" not in {c["name"] for c in sa.inspect(conn).get_columns("probe")}
    engine.dispose()
