"""Registry path validation.

The directory case is not hypothetical: a Docker single-file bind mount whose
host file does not exist makes the daemon create a *directory* at the mount
point, and the app then fails with a bare "Is a directory" OSError that says
nothing about the cause. These tests pin the actionable messages.
"""

from __future__ import annotations

import pytest

from orchestrator.registry.loader import RegistryPathError, validate_registry_path


def test_directory_gives_actionable_docker_hint(tmp_path):
    as_dir = tmp_path / "services.yaml"
    as_dir.mkdir()

    with pytest.raises(RegistryPathError) as exc:
        validate_registry_path(as_dir)

    msg = str(exc.value)
    assert "DIRECTORY" in msg
    assert "bind mount" in msg
    assert "rmdir" in msg  # tells the operator exactly how to recover


def test_missing_file_says_how_to_create_it(tmp_path):
    with pytest.raises(RegistryPathError) as exc:
        validate_registry_path(tmp_path / "nope.yaml")

    assert "does not exist" in str(exc.value)
    assert "services.example.yaml" in str(exc.value)


def test_regular_file_passes(tmp_path):
    ok = tmp_path / "services.yaml"
    ok.write_text("version: 1\n", encoding="utf-8")
    validate_registry_path(ok)  # must not raise
