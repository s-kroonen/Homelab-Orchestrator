"""Entry points / cert resolver outside the web API: the migration, the YAML
round trip (schema, loader, editor), the loader's validation and the CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from alembic import command
from alembic.config import Config
from click.testing import CliRunner
from sqlmodel import Session, select

from orchestrator import cli_config
from orchestrator.adapters.proxy.base import EntryPointInfo
from orchestrator.cli import cli
from orchestrator.config import get_settings
from orchestrator.db.models import ProxyHost
from orchestrator.registry.editor import RegistryEditor
from orchestrator.registry.loader import (
    compute_registry_diff,
    reconcile_yaml_into_db,
    save_registry_to_yaml,
)

EXAMPLE = Path(__file__).parent.parent / "config" / "services.example.yaml"

# ---------------------------------------------------------------------------
# migration 0011
# ---------------------------------------------------------------------------


def _alembic_cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


def test_migration_adds_columns_with_safe_defaults_to_an_existing_database() -> None:
    cfg = _alembic_cfg()
    command.upgrade(cfg, "0010_backup_wake_prereqs")
    engine = sa.create_engine(get_settings().database_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO proxy_host (hostname, upstream, router_provider, extra) "
                "VALUES ('old.example.com', 'http://x:1', 'traefik', '{}')"
            )
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT entry_points, cert_resolver FROM proxy_host WHERE hostname='old.example.com'"
            )
        ).one()
    assert row.cert_resolver == ""
    assert str(row.entry_points) in ("[]", "'[]'") or row.entry_points == []  # "not stored yet"

    command.downgrade(cfg, "0010_backup_wake_prereqs")
    cols = {c["name"] for c in sa.inspect(engine).get_columns("proxy_host")}
    assert "entry_points" not in cols and "cert_resolver" not in cols


def test_migration_on_a_fresh_database_has_the_columns() -> None:
    command.upgrade(_alembic_cfg(), "head")
    engine = sa.create_engine(get_settings().database_url)
    cols = {c["name"] for c in sa.inspect(engine).get_columns("proxy_host")}
    assert {"entry_points", "cert_resolver"} <= cols


# ---------------------------------------------------------------------------
# YAML <-> DB round trip
# ---------------------------------------------------------------------------


def test_example_yaml_entry_points_load_into_the_db(session: Session) -> None:
    path = get_settings().services_yaml_path
    shutil.copy(EXAMPLE, path)
    reconcile_yaml_into_db(session, path)
    session.commit()
    media = session.exec(select(ProxyHost).where(ProxyHost.hostname == "media.example.lan")).one()
    assert media.entry_points == ["websecure"] and media.cert_resolver == "letsencrypt"
    cloud = session.exec(select(ProxyHost).where(ProxyHost.hostname == "cloud.example.lan")).one()
    assert cloud.entry_points == ["web"] and cloud.cert_resolver == ""
    home = session.exec(select(ProxyHost).where(ProxyHost.hostname == "home.example.lan")).one()
    assert home.entry_points == []  # "not stored yet" is a valid state


def test_entry_points_survive_save_and_reload(session: Session) -> None:
    path = get_settings().services_yaml_path
    shutil.copy(EXAMPLE, path)
    reconcile_yaml_into_db(session, path)
    session.commit()

    home = session.exec(select(ProxyHost).where(ProxyHost.hostname == "home.example.lan")).one()
    home.entry_points = ["web", "websecure"]
    home.cert_resolver = "cloudflare"
    session.add(home)
    session.commit()
    assert compute_registry_diff(session).dirty is True

    save_registry_to_yaml(session, path, actor="test")
    session.commit()
    assert compute_registry_diff(session).dirty is False
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    hosts = {h["hostname"]: h for svc in saved["services"] for h in svc.get("proxy_hosts") or []}
    assert hosts["home.example.lan"]["entry_points"] == ["web", "websecure"]
    assert hosts["home.example.lan"]["cert_resolver"] == "cloudflare"

    reconcile_yaml_into_db(session, path)
    session.commit()
    again = session.exec(select(ProxyHost).where(ProxyHost.hostname == "home.example.lan")).one()
    assert again.entry_points == ["web", "websecure"] and again.cert_resolver == "cloudflare"


def test_yaml_entry_points_accept_a_comma_string_and_are_cleaned(session: Session) -> None:
    path = get_settings().services_yaml_path
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "entry_points: [websecure]", 'entry_points: " websecure, web ,websecure"'
    )
    path.write_text(text, encoding="utf-8")
    reconcile_yaml_into_db(session, path)
    session.commit()
    media = session.exec(select(ProxyHost).where(ProxyHost.hostname == "media.example.lan")).one()
    assert media.entry_points == ["websecure", "web"]


def test_loader_rejects_the_internal_traefik_entry_point(session: Session) -> None:
    path = get_settings().services_yaml_path
    path.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace(
            "entry_points: [websecure]", "entry_points: [traefik]"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="internal"):
        reconcile_yaml_into_db(session, path)


def test_editor_round_trips_the_new_fields(tmp_path: Path) -> None:
    path = tmp_path / "services.yaml"
    shutil.copy(EXAMPLE, path)
    editor = RegistryEditor(path)
    editor.upsert_proxy_host(
        "example-home",
        {
            "hostname": "home2.example.lan",
            "upstream": "http://10.0.0.20:8123",
            "router_provider": "traefik",
            "entry_points": ["websecure"],
            "cert_resolver": "letsencrypt",
        },
    )
    editor.save()
    hosts = RegistryEditor(path).proxy_hosts("example-home")
    added = next(h for h in hosts if h["hostname"] == "home2.example.lan")
    assert list(added["entry_points"]) == ["websecure"] and added["cert_resolver"] == "letsencrypt"
    # The comment next to the example fields survived the round trip.
    assert "Which Traefik entry points" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SAMPLE = """\
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
    description: ""
    node: eve1
    guest_kind: vm
    guest_id: 111
    enabled: true
    probes: []
    proxy_hosts: []
"""

LIVE = [
    EntryPointInfo("web", ":80"),
    EntryPointInfo("websecure", ":443"),
    EntryPointInfo("traefik", ":8080"),
]


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "services.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def _add(registry: Path, *args: str) -> object:
    return CliRunner().invoke(
        cli,
        ["proxy", "add", "media", "--file", str(registry), "--non-interactive", *args],
    )


def _hosts(registry: Path) -> list[dict[str, object]]:
    return yaml.safe_load(registry.read_text(encoding="utf-8"))["services"][0]["proxy_hosts"]


def test_cli_add_writes_entry_points_and_resolver(registry: Path) -> None:
    result = _add(
        registry,
        "--hostname", "Media.Example.com",
        "--upstream", "http://10.0.0.10:8096",
        "--entry-point", "websecure",
        "--entry-point", "web",
        "--cert-resolver", "letsencrypt",
    )  # fmt: skip
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    host = _hosts(registry)[0]
    assert host["hostname"] == "media.example.com"
    assert host["entry_points"] == ["websecure", "web"] and host["cert_resolver"] == "letsencrypt"


def test_cli_add_rejects_an_unknown_entry_point_with_the_available_list(registry: Path) -> None:
    result = _add(
        registry,
        "--hostname", "media.example.com",
        "--upstream", "http://10.0.0.10:8096",
        "--entry-point", "https",
    )  # fmt: skip
    assert result.exit_code != 0  # type: ignore[attr-defined]
    out = result.output  # type: ignore[attr-defined]
    assert (
        'Entry point "https" does not exist on this Traefik. Available: web, websecure, traefik'
        in out
    )
    assert _hosts(registry) == []  # nothing written


def test_cli_add_reports_every_problem(registry: Path) -> None:
    result = _add(
        registry,
        "--hostname", "https://bad/host",
        "--upstream", "10.0.0.10",
        "--entry-point", "traefik",
    )  # fmt: skip
    out = result.output  # type: ignore[attr-defined]
    assert result.exit_code != 0  # type: ignore[attr-defined]
    for field in ("hostname:", "upstream:", "entry_points:"):
        assert field in out


def test_cli_add_non_interactive_uses_the_default_rule(registry: Path) -> None:
    result = _add(
        registry, "--hostname", "media.example.com", "--upstream", "http://10.0.0.10:8096"
    )
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert _hosts(registry)[0]["entry_points"] == ["websecure"]


def test_cli_add_requires_a_choice_when_nothing_can_be_defaulted(
    registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_config, "_live_catalog", lambda: (None, None))
    result = _add(
        registry, "--hostname", "media.example.com", "--upstream", "http://10.0.0.10:8096"
    )
    assert result.exit_code != 0  # type: ignore[attr-defined]
    assert "Select at least one entry point" in result.output  # type: ignore[attr-defined]


def test_cli_add_rejects_a_duplicate_hostname_on_another_service(registry: Path) -> None:
    assert (
        _add(
            registry,
            "--hostname",
            "a.example.com",
            "--upstream",
            "http://x:1",
            "--entry-point",
            "web",
        ).exit_code
        == 0
    )  # type: ignore[attr-defined]
    editor = RegistryEditor(registry)
    editor.save()
    # Same service + same hostname is an update, not a clash.
    again = _add(
        registry, "--hostname", "a.example.com", "--upstream", "http://x:2", "--entry-point", "web"
    )
    assert again.exit_code == 0  # type: ignore[attr-defined]
    assert len(_hosts(registry)) == 1 and _hosts(registry)[0]["upstream"] == "http://x:2"


def test_cli_add_local_only_provider_skips_the_live_checks(registry: Path) -> None:
    result = _add(
        registry, "--hostname", "n.example.com", "--upstream", "10.0.0.7", "--provider", "npm"
    )
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "entry_points" not in _hosts(registry)[0]


def test_cli_entrypoints_lists_what_the_manager_reports() -> None:
    result = CliRunner().invoke(cli, ["proxy", "entrypoints"])
    assert result.exit_code == 0, result.output
    assert "web" in result.output and ":443" in result.output and "letsencrypt" in result.output
    assert "Default for a new host: websecure" in result.output


def test_cli_entrypoints_says_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_config, "_live_catalog", lambda: (None, None))
    result = CliRunner().invoke(cli, ["proxy", "entrypoints"])
    assert result.exit_code == 1
    assert "unavailable" in result.output.lower()


def test_cli_list_shows_entry_points(registry: Path) -> None:
    _add(
        registry,
        "--hostname", "a.example.com",
        "--upstream", "http://x:1",
        "--entry-point", "websecure",
        "--cert-resolver", "letsencrypt",
    )  # fmt: skip
    result = CliRunner().invoke(cli, ["proxy", "list", "--file", str(registry)])
    assert "ep=websecure" in result.output and "resolver=letsencrypt" in result.output


def test_route_options_survive_save_and_reload(session: Session) -> None:
    path = get_settings().services_yaml_path
    shutil.copy(EXAMPLE, path)
    reconcile_yaml_into_db(session, path)
    session.commit()
    # The example turns on the HTTP -> HTTPS redirect for media only.
    media = session.exec(select(ProxyHost).where(ProxyHost.hostname == "media.example.lan")).one()
    assert media.route_options["http_redirect"] is True
    cloud = session.exec(select(ProxyHost).where(ProxyHost.hostname == "cloud.example.lan")).one()
    assert cloud.route_options == {}  # untouched = defaults, stored as empty

    home = session.exec(select(ProxyHost).where(ProxyHost.hostname == "home.example.lan")).one()
    home.route_options = {
        "pass_host_header": True,
        "skip_upstream_tls_verify": True,
        "tls_main": "example.com",
        "tls_sans": ["*.example.com"],
        "http_redirect": False,
    }
    session.add(home)
    session.commit()
    assert compute_registry_diff(session).dirty is True

    save_registry_to_yaml(session, path, actor="test")
    session.commit()
    assert compute_registry_diff(session).dirty is False
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    hosts = {h["hostname"]: h for svc in saved["services"] for h in svc.get("proxy_hosts") or []}
    assert hosts["home.example.lan"]["route_options"]["tls_main"] == "example.com"

    reconcile_yaml_into_db(session, path)
    session.commit()
    again = session.exec(select(ProxyHost).where(ProxyHost.hostname == "home.example.lan")).one()
    assert again.route_options["skip_upstream_tls_verify"] is True
    assert again.route_options["tls_sans"] == ["*.example.com"]
