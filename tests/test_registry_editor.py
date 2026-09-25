"""Surgical YAML editing: preserve what the operator wrote, validate before saving."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.registry.editor import RegistryEditError, RegistryEditor
from orchestrator.registry.probe_specs import PROBE_SPECS, kinds_by_menu_order

SAMPLE = """\
---
# My homelab registry — hand-written notes live here.
version: 1

nodes:
  # eve1 is the always-on node.
  - name: eve1
    always_on: true
    power_mgr_target: eve1
    notes: "gateway lives here"

backup_policies:
  - name: daily-frequent
    schedule_cron: "0 3 * * *"
    mode: snapshot
    retention:
      keep_daily: 7
    targets: {}

services:
  # Home Assistant — the important one.
  - slug: haos
    name: Home Assistant
    description: ""
    node: eve1
    guest_kind: vm
    guest_id: 111
    enabled: true
    backup_policy: daily-frequent
    probes: []
    proxy_hosts: []
"""


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "services.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The property that matters most: don't destroy the operator's file
# ---------------------------------------------------------------------------


def test_editing_preserves_comments(registry: Path) -> None:
    """A CLI edit must not cost the operator their annotations.

    This is why edits mutate the ruamel document instead of dumping a Pydantic
    model over the file.
    """
    editor = RegistryEditor(registry)
    editor.upsert_probe(
        "haos",
        {
            "name": "http",
            "kind": "http",
            "required": True,
            "timeout_s": 10,
            "order": 10,
            "config": {"url": "https://haos.lan/"},
        },
    )
    editor.save()

    text = registry.read_text(encoding="utf-8")
    assert "# My homelab registry" in text
    assert "# eve1 is the always-on node." in text
    assert "# Home Assistant — the important one." in text
    assert "https://haos.lan/" in text


def test_unrelated_fields_survive_an_update(registry: Path) -> None:
    """Updating one field must not reset the others."""
    editor = RegistryEditor(registry)
    editor.upsert_service("haos", {"backup_excluded": True})
    editor.save()

    reloaded = RegistryEditor(registry)
    svc = reloaded.require_service("haos")
    assert svc["backup_excluded"] is True
    assert svc["guest_id"] == 111  # untouched
    assert svc["name"] == "Home Assistant"
    assert svc["backup_policy"] == "daily-frequent"


def test_invalid_edit_is_refused_before_touching_disk(registry: Path) -> None:
    """A bad edit must leave the previous file intact, not a broken one."""
    before = registry.read_text(encoding="utf-8")
    editor = RegistryEditor(registry)
    # A service pointing at a node the file does not define.
    editor.upsert_service("orphan", {"name": "Orphan", "node": "does-not-exist"})

    with pytest.raises(RegistryEditError, match="invalid"):
        editor.save()

    assert registry.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def test_upsert_reports_added_then_updated(registry: Path) -> None:
    editor = RegistryEditor(registry)
    assert editor.upsert_service("newthing", {"name": "New", "node": "eve1"}) == "added"
    assert editor.upsert_service("newthing", {"name": "Renamed"}) == "updated"
    assert editor.require_service("newthing")["name"] == "Renamed"


def test_new_services_get_probe_and_proxy_lists(registry: Path) -> None:
    """So `probe add` and `proxy add` have somewhere to write immediately."""
    editor = RegistryEditor(registry)
    editor.upsert_service("newthing", {"name": "New", "node": "eve1"})
    svc = editor.require_service("newthing")
    assert svc["probes"] == []
    assert svc["proxy_hosts"] == []


def test_remove_service(registry: Path) -> None:
    editor = RegistryEditor(registry)
    assert editor.remove_service("haos") is True
    assert editor.remove_service("haos") is False
    editor.save()
    assert "haos" not in RegistryEditor(registry).service_slugs()


def test_require_service_lists_known_slugs(registry: Path) -> None:
    editor = RegistryEditor(registry)
    with pytest.raises(RegistryEditError) as exc:
        editor.require_service("nope")
    assert "haos" in str(exc.value)  # tells you what IS there


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _probe(name: str = "http", url: str = "https://x.lan/") -> dict:
    return {
        "name": name,
        "kind": "http",
        "required": True,
        "timeout_s": 10,
        "order": 10,
        "config": {"url": url},
    }


def test_probe_add_then_replace_by_name(registry: Path) -> None:
    editor = RegistryEditor(registry)
    assert editor.upsert_probe("haos", _probe()) == "added"
    assert editor.upsert_probe("haos", _probe(url="https://changed/")) == "updated"
    probes = editor.probes("haos")
    assert len(probes) == 1  # replaced, not duplicated
    assert probes[0]["config"]["url"] == "https://changed/"


def test_probe_remove(registry: Path) -> None:
    editor = RegistryEditor(registry)
    editor.upsert_probe("haos", _probe())
    assert editor.remove_probe("haos", "http") is True
    assert editor.remove_probe("haos", "http") is False


def test_probe_without_a_name_is_refused(registry: Path) -> None:
    editor = RegistryEditor(registry)
    with pytest.raises(RegistryEditError, match="name"):
        editor.upsert_probe("haos", {"kind": "http", "config": {}})


def test_saved_probe_survives_a_reload(registry: Path) -> None:
    editor = RegistryEditor(registry)
    editor.upsert_probe("haos", _probe())
    editor.save()
    assert RegistryEditor(registry).probes("haos")[0]["kind"] == "http"


# ---------------------------------------------------------------------------
# Proxy hosts
# ---------------------------------------------------------------------------


def test_proxy_host_round_trip(registry: Path) -> None:
    editor = RegistryEditor(registry)
    entry = {
        "hostname": "haos.example.com",
        "upstream": "http://10.0.0.5:8123",
        "router_provider": "traefik",
    }
    assert editor.upsert_proxy_host("haos", entry) == "added"
    editor.save()

    hosts = RegistryEditor(registry).proxy_hosts("haos")
    assert hosts[0]["hostname"] == "haos.example.com"
    assert hosts[0]["upstream"] == "http://10.0.0.5:8123"


def test_proxy_host_replaced_by_hostname(registry: Path) -> None:
    editor = RegistryEditor(registry)
    editor.upsert_proxy_host("haos", {"hostname": "h.lan", "upstream": "http://a"})
    assert editor.upsert_proxy_host("haos", {"hostname": "h.lan", "upstream": "http://b"}) == (
        "updated"
    )
    assert len(editor.proxy_hosts("haos")) == 1


def test_proxy_host_remove(registry: Path) -> None:
    editor = RegistryEditor(registry)
    editor.upsert_proxy_host("haos", {"hostname": "h.lan", "upstream": "http://a"})
    assert editor.remove_proxy_host("haos", "h.lan") is True
    assert editor.remove_proxy_host("haos", "h.lan") is False


# ---------------------------------------------------------------------------
# Nodes / policies / new files
# ---------------------------------------------------------------------------


def test_ensure_node_is_idempotent(registry: Path) -> None:
    editor = RegistryEditor(registry)
    assert editor.ensure_node("eve1") is False  # already present, nothing changed
    assert editor.ensure_node("eve2") is True
    assert set(editor.node_names()) == {"eve1", "eve2"}


def test_ensure_node_updates_always_on(registry: Path) -> None:
    editor = RegistryEditor(registry)
    assert editor.ensure_node("eve1", always_on=False) is True
    assert editor.doc["nodes"][0]["always_on"] is False


def test_a_missing_file_starts_a_valid_empty_registry(tmp_path: Path) -> None:
    editor = RegistryEditor(tmp_path / "new.yaml")
    editor.ensure_node("eve1", always_on=True)
    editor.upsert_service("x", {"name": "X", "node": "eve1"})
    editor.save()
    assert (tmp_path / "new.yaml").exists()
    assert RegistryEditor(tmp_path / "new.yaml").service_slugs() == ["x"]


def test_a_directory_gives_the_docker_mount_hint(tmp_path: Path) -> None:
    as_dir = tmp_path / "services.yaml"
    as_dir.mkdir()
    with pytest.raises(RegistryEditError, match="bind mount"):
        RegistryEditor(as_dir)


# ---------------------------------------------------------------------------
# Probe spec table
# ---------------------------------------------------------------------------


def test_every_probe_kind_is_offered_by_the_cli() -> None:
    """A kind the builder cannot construct would be unreachable without hand-editing."""
    from orchestrator.domain.enums import ProbeKind

    missing = {k.value for k in ProbeKind} - {k.value for k in PROBE_SPECS}
    assert not missing, f"probe kinds the CLI cannot build: {sorted(missing)}"


def test_menu_order_covers_every_spec() -> None:
    assert set(kinds_by_menu_order()) == set(PROBE_SPECS)


def test_required_fields_have_no_misleading_default() -> None:
    """A required field showing a default would let the operator accept an
    invalid config by pressing enter."""
    for kind, spec in PROBE_SPECS.items():
        for field in spec.fields:
            if field.required:
                assert field.default is None, f"{kind.value}.{field.key} is required but defaulted"
