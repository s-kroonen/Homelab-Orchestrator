"""The wizard's answers: connector keys become connections, the rest stays in .env."""

from __future__ import annotations

from pathlib import Path

from orchestrator.connections import envmap
from orchestrator.connections.model import Connection
from orchestrator.connections.store import read_file, write_file
from orchestrator.setup.envfile import default_values, read_env
from orchestrator.setup.project import connections_path, read_values, save_values


def _values(**kw: str) -> dict[str, str]:
    return {**default_values(), **kw}


def test_only_filled_in_kinds_become_connections() -> None:
    conns = envmap.from_values(
        _values(
            PROXMOX_HOST="pve.lan",
            PROXMOX_TOKEN_ID="root@pam!orch",
            PROXMOX_TOKEN_SECRET="s3cret",
            PROXMOX_PORT="8006",  # the default: not written
        )
    )
    assert [c.kind for c in conns] == ["proxmox"]
    pve = conns[0]
    assert pve.enabled and pve.settings == {"host": "pve.lan", "token_id": "root@pam!orch"}
    assert pve.secrets == {"token_secret": "s3cret"}


def test_a_half_filled_connection_is_kept_but_switched_off() -> None:
    (pbs,) = envmap.from_values(_values(PBS_HOST="pbs.lan"))  # no token yet
    assert pbs.kind == "pbs" and pbs.enabled is False and pbs.settings["host"] == "pbs.lan"
    (only_probe,) = envmap.from_values(_values(PROBE_PROXY_BASE_URL="https://10.0.0.2"))
    assert only_probe.enabled is False  # typed, so not lost, but there is no Traefik Manager yet
    assert envmap.to_values([only_probe])["PROBE_PROXY_BASE_URL"] == "https://10.0.0.2"


def test_round_trip() -> None:
    values = _values(
        PROXMOX_HOST="pve.lan",
        PROXMOX_TOKEN_ID="root@pam!orch",
        PROXMOX_TOKEN_SECRET="s3cret",
        PROXMOX_VERIFY_TLS="false",
    )
    back = envmap.to_values(envmap.from_values(values))
    for key in ("PROXMOX_HOST", "PROXMOX_TOKEN_SECRET", "PROXMOX_VERIFY_TLS"):
        assert back[key] == values[key]


def test_save_splits_env_from_connections(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    values = _values(
        DRY_RUN="false",
        PROXMOX_HOST="pve.lan",
        PROXMOX_TOKEN_ID="root@pam!orch",
        PROXMOX_TOKEN_SECRET="s3cret",
    )
    save_values(values, env)
    raw = read_env(env)
    assert raw["DRY_RUN"] == "false" and "PROXMOX_HOST" not in raw
    assert "s3cret" not in env.read_text(encoding="utf-8")
    file = connections_path(env)
    assert [c.id for c in read_file(file)] == ["proxmox"]
    assert read_values(env)["PROXMOX_TOKEN_SECRET"] == "s3cret"


def test_saving_again_keeps_what_was_named_in_the_dashboard(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    path = connections_path(env)
    write_file(
        path,
        [
            Connection(
                id="proxmox",
                kind="proxmox",
                name="Home cluster",
                site="home",
                scope="shared",
                settings={"host": "old.lan", "token_id": "a@b!c"},
                secrets={"token_secret": "x"},
            ),
            Connection(
                id="offsite",
                kind="portainer",
                settings={"url": "https://off.example"},
                secrets={"api_key": "k"},
            ),
        ],
    )
    values = read_values(env)
    values["PROXMOX_HOST"] = "new.lan"
    save_values(values, env)
    by_id = {c.id: c for c in read_file(path)}
    assert by_id["proxmox"].settings["host"] == "new.lan"
    assert (by_id["proxmox"].name, by_id["proxmox"].site, by_id["proxmox"].scope) == (
        "Home cluster",
        "home",
        "shared",
    )
    assert "offsite" in by_id  # another kind in the file is untouched


def test_the_wizard_edits_the_connection_in_use_not_a_new_one(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    write_file(
        connections_path(env),
        [
            Connection(
                id="pve-main",
                kind="proxmox",
                settings={"host": "a.lan", "token_id": "a@b!c"},
                secrets={"token_secret": "x"},
            )
        ],
    )
    other = _values(PROXMOX_HOST="b.lan", PROXMOX_TOKEN_ID="a@b!c", PROXMOX_TOKEN_SECRET="y")
    save_values(other, env)
    (only,) = read_file(connections_path(env))
    assert only.id == "pve-main" and only.settings["host"] == "b.lan"


def test_old_env_connector_keys_are_moved_on_the_next_save(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "PORTAINER_URL=https://p.example:9443\nPORTAINER_API_KEY=key\nLOG_LEVEL=DEBUG\n",
        encoding="utf-8",
    )
    values = {**default_values(), **read_values(env)}
    save_values(values, env)
    raw = read_env(env)
    assert raw["LOG_LEVEL"] == "DEBUG" and "PORTAINER_URL" not in raw
    (conn,) = read_file(connections_path(env))
    assert conn.kind == "portainer" and conn.secrets == {"api_key": "key"}
