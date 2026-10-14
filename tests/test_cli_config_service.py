"""``orchestrator-cli service update`` — the CLI path for per-service fields,
``--group`` included. The YAML-editing behaviour itself (comment preservation,
validation-before-save) is covered by ``test_registry_editor.py``; this just
proves the CLI wires its flags into ``RegistryEditor.upsert_service`` the way
the existing fields (``--policy``, ``--depends-on``) already do.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from orchestrator.cli import cli

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


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "services.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def _run(*args: str) -> object:
    return CliRunner().invoke(cli, list(args))


def test_service_update_sets_group(registry: Path) -> None:
    result = _run("service", "update", "media", "--group", "entertainment", "--file", str(registry))
    assert result.exit_code == 0, result.output

    doc = yaml.safe_load(registry.read_text(encoding="utf-8"))
    svc = next(s for s in doc["services"] if s["slug"] == "media")
    assert svc["group"] == "entertainment"


def test_service_update_clears_group_with_empty_string(registry: Path) -> None:
    _run("service", "update", "media", "--group", "entertainment", "--file", str(registry))
    result = _run("service", "update", "media", "--group", "", "--file", str(registry))
    assert result.exit_code == 0, result.output

    doc = yaml.safe_load(registry.read_text(encoding="utf-8"))
    svc = next(s for s in doc["services"] if s["slug"] == "media")
    assert svc["group"] == ""
