"""The .env layout, its quoting, and safe writes.

.env holds every credential the orchestrator has, and git cannot restore it. These
pin what protects it: nothing is written that a reader would parse differently,
and the previous file is always copied aside before it is replaced.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest
from dotenv import dotenv_values

from orchestrator.config import Settings
from orchestrator.setup.envfile import (
    EnvFileError,
    backup_file,
    default_values,
    format_value,
    layout_names,
    read_env,
    render_env,
    render_example,
    write_env,
)

REPO = Path(__file__).resolve().parents[1]


def test_every_setting_is_in_the_layout() -> None:
    missing = {name.upper() for name in Settings.model_fields} - set(layout_names())
    assert not missing, f"add these to ENV_SECTIONS: {sorted(missing)}"


def test_layout_names_are_unique() -> None:
    names = layout_names()
    assert len(names) == len(set(names))


def test_env_example_is_generated_from_the_layout() -> None:
    committed = (REPO / ".env.example").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == render_example(), "regenerate it: python -m orchestrator.setup.envfile"


def test_defaults_offer_no_placeholder_hosts() -> None:
    """A placeholder offered as a wizard default is one Enter away from being written."""
    values = default_values()
    for key in ("PROXMOX_HOST", "PBS_HOST", "MQTT_HOST", "PBS_DATASTORE", "PVE_BACKUP_STORAGE"):
        assert "example" not in values[key], key


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        "orchestrator@pve!backups",
        "with space",
        "hash # inside",
        "dollar$sign",
        'double"quote',
        "back\\slash",
        "a=b",
        "it's",
    ],
)
def test_values_round_trip_through_dotenv(tmp_path: Path, value: str) -> None:
    path = tmp_path / ".env"
    path.write_text(render_env({"PROXMOX_TOKEN_SECRET": value}, "header"), encoding="utf-8")
    assert dotenv_values(path)["PROXMOX_TOKEN_SECRET"] == value


@pytest.mark.parametrize(
    "value",
    [
        "it's got a space",
        "'leading",
        "${EXPANDED_BY_DOTENV}",
        "trailing\\",
        "double\\\\backslash",
        "line\nbreak",
    ],
)
def test_values_readers_would_disagree_on_are_refused(value: str) -> None:
    with pytest.raises(EnvFileError):
        format_value("X", value)


def test_keys_added_by_hand_are_kept() -> None:
    text = render_env({"DRY_RUN": "true", "MY_OWN_SETTING": "1"}, "header")
    assert "MY_OWN_SETTING=1" in text


def test_write_env_backs_up_the_previous_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("PROXMOX_TOKEN_SECRET=old-secret\n", encoding="utf-8")

    backup = write_env(env, {**default_values(), "PROXMOX_TOKEN_SECRET": "new-secret"})

    assert backup is not None
    assert backup.read_text(encoding="utf-8") == "PROXMOX_TOKEN_SECRET=old-secret\n"
    assert read_env(env)["PROXMOX_TOKEN_SECRET"] == "new-secret"
    assert not (tmp_path / ".env.tmp").exists()


def test_a_bad_value_fails_before_anything_is_touched(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("KEEP=1\n", encoding="utf-8")

    with pytest.raises(EnvFileError):
        write_env(env, {"PROXMOX_TOKEN_SECRET": "bad'value with space"})

    assert env.read_text(encoding="utf-8") == "KEEP=1\n"
    assert list(tmp_path.iterdir()) == [env]  # no backup, no temp file


def test_backups_never_overwrite_each_other(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("first", encoding="utf-8")
    first = backup_file(path)
    path.write_text("second", encoding="utf-8")
    second = backup_file(path)

    assert first is not None and second is not None and first != second
    assert first.read_text(encoding="utf-8") == "first"
    assert second.read_text(encoding="utf-8") == "second"


def test_an_empty_file_is_not_backed_up(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    assert backup_file(path) is None


@pytest.mark.parametrize("name", [".env", "services.yaml"])
def test_backup_names_are_git_ignored(tmp_path: Path, name: str) -> None:
    """A backup holds the same secrets and addresses as the original."""
    path = tmp_path / name
    path.write_text("x", encoding="utf-8")
    backup = backup_file(path)
    assert backup is not None

    patterns = [
        line.strip()
        for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith(("#", "!"))
    ]
    assert any(fnmatch.fnmatch(backup.name, p) for p in patterns), backup.name


def test_read_env_rejects_a_directory(tmp_path: Path) -> None:
    (tmp_path / ".env").mkdir()
    with pytest.raises(EnvFileError, match="directory"):
        read_env(tmp_path / ".env")
