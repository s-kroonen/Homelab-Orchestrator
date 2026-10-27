"""Connections: the file, the rules over it, and how it lands on Settings."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from orchestrator import config as config_module
from orchestrator.config import get_settings, reload_settings
from orchestrator.connections import runtime
from orchestrator.connections.model import Connection, ConnectionError
from orchestrator.connections.store import (
    ConnectionsFileError,
    check_set,
    env_connections,
    path_for,
    primary,
    read_file,
    write_file,
)


def portainer(ident: str = "portainer", **kw: object) -> Connection:
    settings = {"url": "https://10.0.0.2:9443", "verify_tls": False, **kw.pop("settings", {})}  # type: ignore[arg-type]
    secrets = {"api_key": "ptr_secret", **kw.pop("secrets", {})}  # type: ignore[arg-type]
    return Connection(id=ident, kind="portainer", settings=settings, secrets=secrets, **kw)  # type: ignore[arg-type]


def test_a_connection_is_validated_against_its_kind() -> None:
    portainer().validate_values()
    with pytest.raises(ConnectionError, match="not a field"):
        Connection(id="p", kind="portainer", settings={"nope": 1}).validate_values()
    with pytest.raises(ConnectionError, match="belongs in secrets"):
        Connection(
            id="p", kind="portainer", settings={"api_key": "x", "url": "u"}
        ).validate_values()
    with pytest.raises(ConnectionError, match="is required"):
        Connection(id="p", kind="portainer", secrets={"api_key": "x"}).validate_values()
    # a switched-off draft may be incomplete
    Connection(id="p", kind="portainer", enabled=False).validate_values()
    with pytest.raises(ConnectionError, match="id"):
        portainer("Bad Id").validate_values()
    with pytest.raises(ConnectionError, match="not a number"):
        Connection(id="m", kind="power", settings={"host": "h", "port": "abc"}).validate_values()


def test_values_are_typed_and_secrets_are_never_reported(tmp_path: Path) -> None:
    conn = Connection(
        id="pve",
        kind="proxmox",
        settings={"host": "pve", "port": "8007", "verify_tls": "no", "token_id": "a@pam!t"},
        secrets={"token_secret": "s3cret"},
    )
    conn.validate_values()
    assert conn.settings["port"] == 8007 and conn.settings["verify_tls"] is False
    public = conn.public()
    assert "s3cret" not in str(public)
    assert public["secrets_set"]["token_secret"] is True
    assert public["secrets_set"]["restore_token_secret"] is False
    assert public["settings"]["port"] == 8007 and public["settings"]["backup_storage"] == ""


def test_the_file_round_trips_private_and_keeps_the_previous_copy(tmp_path: Path) -> None:
    path = tmp_path / "connections.yaml"
    assert read_file(path) == []
    assert write_file(path, [portainer()]) is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    back = read_file(path)
    assert [c.id for c in back] == ["portainer"] and back[0].secrets == {"api_key": "ptr_secret"}
    assert back[0].settings["verify_tls"] is False

    backup = write_file(path, [portainer(), portainer("offsite", site="offsite", scope="shared")])
    assert backup is not None and backup.parent.name == "connections-backups"
    assert [c.id for c in read_file(path)] == ["portainer", "offsite"]
    assert read_file(path)[1].scope == "shared" and read_file(path)[1].site == "offsite"


def test_a_broken_file_is_reported_not_trusted(tmp_path: Path) -> None:
    path = tmp_path / "connections.yaml"
    path.write_text("connections:\n  - id: p\n    kind: portainer\n    settings: {bogus: 1}\n")
    with pytest.raises(ConnectionsFileError, match="bogus"):
        read_file(path)
    path.write_text("connections: [")
    with pytest.raises(ConnectionsFileError, match="not valid YAML"):
        read_file(path)


def test_rules_over_the_whole_set() -> None:
    assert check_set([portainer(), portainer("second")]) == []  # Portainer may repeat
    assert any("called 'portainer'" in p for p in check_set([portainer(), portainer()]))
    pve = lambda i, **kw: Connection(  # noqa: E731
        id=i,
        kind="proxmox",
        settings={"host": "h", "token_id": "t"},
        secrets={"token_secret": "s"},
        **kw,
    )
    assert any("only one Proxmox VE" in p for p in check_set([pve("a"), pve("b")]))
    assert check_set([pve("a"), pve("b", enabled=False)]) == []
    two_defaults = [portainer(default=True), portainer("second", default=True)]
    assert any("default" in p for p in check_set(two_defaults))


def test_the_connection_in_use_is_the_default_else_the_first_enabled() -> None:
    a, b, c = portainer("a"), portainer("b"), portainer("c", enabled=False)
    assert primary([c, a, b], "portainer") is a
    b.default = True
    assert primary([c, a, b], "portainer") is b
    assert primary([c], "portainer") is None
    assert primary([a], "proxmox") is None


def test_env_only_connections_are_worked_out_and_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTAINER_URL", "https://p.example:9443")
    monkeypatch.setenv("PORTAINER_API_KEY", "key-1")
    monkeypatch.setenv("PORTAINER_VERIFY_TLS", "false")
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/mine")
    config_module.reset_settings_cache()
    found = {c.kind: c for c in env_connections(get_settings())}
    assert set(found) == {"portainer", "ntfy"}
    p = found["portainer"]
    assert p.source == "env" and p.settings["url"] == "https://p.example:9443"
    assert p.settings["verify_tls"] is False and p.secrets == {"api_key": "key-1"}
    p.validate_values()  # what is there is a valid connection


def test_the_file_owns_a_kind_and_the_environment_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PORTAINER_URL", "https://old.example:9443")
    monkeypatch.setenv("PORTAINER_API_KEY", "old-key")
    monkeypatch.setenv("HAOS_URL", "https://ha.example")
    monkeypatch.setenv("HAOS_TOKEN", "ha-token")
    config_module.reset_settings_cache()
    write_file(path_for(get_settings()), [portainer(settings={"url": "https://new.example:9443"})])
    config_module.reset_settings_cache()
    s = get_settings()
    assert s.portainer_url == "https://new.example:9443"  # the file
    assert s.portainer_api_key.get_secret_value() == "ptr_secret"
    assert s.portainer_adapter == "api"
    assert s.haos_url == "https://ha.example" and s.haos_token.get_secret_value() == "ha-token"
    snap = runtime.current()
    assert [c.id for c in snap.file] == ["portainer"] and [c.kind for c in snap.env] == ["haos"]


def test_a_kind_whose_connections_are_all_off_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTAINER_URL", "https://old.example:9443")
    monkeypatch.setenv("PORTAINER_API_KEY", "old-key")
    config_module.reset_settings_cache()
    write_file(path_for(get_settings()), [portainer(enabled=False)])
    config_module.reset_settings_cache()
    s = get_settings()
    assert s.portainer_url == "" and s.portainer_api_key.get_secret_value() == ""
    assert s.portainer_adapter == "dry_run"


def test_notifier_channels_come_from_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    config_module.reset_settings_cache()
    write_file(
        path_for(get_settings()),
        [Connection(id="phone", kind="ntfy", settings={"url": "https://ntfy.sh/x"})],
    )
    config_module.reset_settings_cache()
    s = get_settings()
    assert s.ntfy_url == "https://ntfy.sh/x" and s.notifier_adapter == "auto"


def test_one_instances_own_view_leaves_the_others_alone() -> None:
    config_module.reset_settings_cache()
    base = get_settings()
    other = portainer("offsite", settings={"url": "https://off.example:9443", "read_only": True})
    view = runtime.settings_for(base, other)
    assert view.portainer_url == "https://off.example:9443" and view.portainer_read_only is True
    assert view.portainer_api_key.get_secret_value() == "ptr_secret"
    assert base.portainer_url == ""  # untouched


def test_reload_updates_the_live_settings_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    config_module.reset_settings_cache()
    live = get_settings()
    assert live.portainer_url == ""
    write_file(path_for(live), [portainer()])
    assert reload_settings() is live  # the same object everyone already holds
    assert live.portainer_url == "https://10.0.0.2:9443"
    write_file(path_for(live), [portainer(enabled=False)])
    reload_settings()
    assert live.portainer_url == ""


def test_a_broken_file_leaves_the_environment_in_force(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTAINER_URL", "https://env.example:9443")
    monkeypatch.setenv("PORTAINER_API_KEY", "k")
    config_module.reset_settings_cache()
    path_for(get_settings()).write_text("connections: [")
    config_module.reset_settings_cache()
    assert get_settings().portainer_url == "https://env.example:9443"
    assert "not valid YAML" in runtime.current().error


# -- adapters follow the connections ------------------------------------------------------------


async def test_the_bundle_has_one_portainer_per_connection_and_reloads_in_place() -> None:
    from orchestrator.adapters.factory import build_adapters
    from orchestrator.adapters.portainer.shared import get_shared

    config_module.reset_settings_cache()
    live = get_settings()
    write_file(
        path_for(live),
        [
            portainer("home", site="home"),
            portainer("offsite", settings={"url": "https://off.example:9443"}, site="offsite"),
        ],
    )
    reload_settings()
    live.dry_run = False  # build the live adapters (nothing is contacted by building them)
    bundle = build_adapters(live)
    assert sorted(bundle.portainers) == ["home", "offsite"] and bundle.default_portainer == "home"
    assert bundle.portainer_for() is bundle.portainer_for("home") is bundle.portainer
    assert bundle.portainer_for("offsite").base_url == "https://off.example:9443"  # type: ignore[union-attr]
    assert bundle.portainer_for("offsite").instance_id == "offsite"  # type: ignore[union-attr]
    assert bundle.portainer_for("nope") is None
    assert get_shared("offsite") is bundle.portainer_for("offsite")

    # the handles other components hold keep working across a reload
    proxmox_handle = bundle.proxmox
    first = proxmox_handle.target
    write_file(path_for(live), [portainer("home"), portainer("offsite", default=True)])
    reload_settings()
    await bundle.reload(live)
    assert bundle.proxmox is proxmox_handle and bundle.default_portainer == "offsite"
    assert proxmox_handle.target is not first
    assert isinstance(bundle.portainer, type(bundle.portainer_for("offsite")))


async def test_a_check_says_what_is_wrong_without_touching_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator.connections.checks import check_connection

    config_module.reset_settings_cache()
    live = get_settings()
    blank = Connection(id="p", kind="portainer", enabled=False)
    assert (await check_connection(blank, live)).ok is False
    ok = portainer()
    assert (await check_connection(ok, live)).ok is None  # DRY_RUN in tests: not contacted
    live.dry_run = False
    ssh = Connection(id="s", kind="ssh", settings={"key_path": "/nowhere/id_ed25519"})
    result = await check_connection(ssh, live)
    assert result.ok is False and "not there" in result.detail
    ntfy = Connection(id="n", kind="ntfy", settings={"url": "https://ntfy.sh/x"})
    assert (await check_connection(ntfy, live)).ok is None
