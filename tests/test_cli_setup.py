"""init, config and scaffold, against a fake cluster — never the network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from ruamel.yaml import YAML

from orchestrator import cli_setup
from orchestrator import config as config_module
from orchestrator.adapters.errors import AdapterAuthError
from orchestrator.adapters.proxmox.base import BackupStorage, ClusterNode, ClusterStatus, Guest
from orchestrator.cli import cli
from orchestrator.domain.enums import GuestKind
from orchestrator.registry.editor import RegistryEditor
from orchestrator.setup.envfile import read_env

SECRET = "pve-secret-value-0001"

CLUSTER = ClusterStatus(
    nodes=[
        # The node PROXMOX_HOST reaches is a burst node — the mistake init must flag.
        ClusterNode("burst", online=True, local=True, ip="10.0.0.12"),
        ClusterNode("steady", online=True, local=False, ip="10.0.0.11"),
    ],
    quorate=True,
    cluster_name="lab",
)
GUESTS = [
    Guest(node="steady", vmid=100, kind=GuestKind.VM, name="gateway", status="running"),
    Guest(node="burst", vmid=110, kind=GuestKind.VM, name="media", status="running"),
    Guest(node="burst", vmid=120, kind=GuestKind.CT, name="wiki", status="stopped"),
]


class FakeDiscovery:
    def __init__(self) -> None:
        self.guest_list = list(GUESTS)
        self.cluster = CLUSTER
        self.fail_pbs = False
        self.calls = 0

    async def proxmox(self, settings: Any) -> cli_setup.ProxmoxFindings:
        self.calls += 1
        return cli_setup.ProxmoxFindings(
            "8.2", self.cluster, [BackupStorage("pbs-lab", "store1", "10.0.0.20")]
        )

    async def pbs(self, settings: Any) -> cli_setup.PbsFindings:
        self.calls += 1
        if self.fail_pbs:
            raise AdapterAuthError("pbs rejected the API token (403)", status_code=403, body="")
        return cli_setup.PbsFindings("3.2", ["store1"])

    async def datastore(self, settings: Any, name: str) -> None:
        self.calls += 1

    async def guests(self, settings: Any) -> tuple[list[Guest], ClusterStatus]:
        self.calls += 1
        return list(self.guest_list), self.cluster

    def tcp(self, host: str, port: int) -> str | None:
        return None


@pytest.fixture
def discovery(monkeypatch: pytest.MonkeyPatch) -> FakeDiscovery:
    fake = FakeDiscovery()
    monkeypatch.setattr(cli_setup, "make_discovery", lambda: fake)
    return fake


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


def _answers(tmp_path: Path, **extra: str) -> Path:
    values = {
        "PROXMOX_HOST": "10.0.0.12",
        "PROXMOX_TOKEN_ID": "orch@pve!t",
        "PROXMOX_TOKEN_SECRET": SECRET,
        "PBS_HOST": "10.0.0.20",
        "PBS_TOKEN_ID": "orch@pbs!t",
        "PBS_TOKEN_SECRET": "pbs-secret-value-0002",
        "WEBAUTHN_RP_ID": "orch.lab.test",
        "PROBE_PROXY_BASE_URL": "https://10.0.0.2",
        **extra,
    }
    path = tmp_path / "answers.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return path


def _run(*args: str, input: str | None = None) -> Result:
    return CliRunner().invoke(cli, list(args), input=input)


def _init(project: Path, answers: Path, *extra: str) -> Result:
    return _run(
        "init",
        "--dir",
        str(project),
        "--from",
        str(answers),
        "--non-interactive",
        "--force",
        *extra,
    )


def _yaml(path: Path) -> Any:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_writes_env_and_registry_from_the_cluster(
    tmp_path: Path, project: Path, discovery: FakeDiscovery
) -> None:
    result = _init(
        project,
        _answers(tmp_path),
        "--always-on",
        "steady",
        "--include",
        "all",
        "--gateway",
        "gateway",
    )
    assert result.exit_code == 0, result.output

    env = read_env(project / ".env")
    assert env["PROXMOX_TOKEN_SECRET"] == SECRET
    assert env["PBS_DATASTORE"] == "store1"  # the only datastore PBS listed
    assert env["PVE_BACKUP_STORAGE"] == "pbs-lab"  # worked out from PVE, not asked
    assert env["GATEWAY_SERVICE"] == "gateway"
    assert env["SERVICES_YAML_PATH"] == "/etc/orchestrator/services.yaml"
    assert len(env["SESSION_SECRET"]) > 40

    registry = _yaml(project / "config" / "services.yaml")
    assert {n["name"]: n["always_on"] for n in registry["nodes"]} == {
        "burst": False,
        "steady": True,
    }
    services = {s["slug"]: s for s in registry["services"]}
    assert set(services) == {"gateway", "media", "wiki"}
    assert services["media"]["depends_on"] == ["gateway"]
    assert "depends_on" not in services["gateway"]
    assert services["gateway"]["probes"][0]["config"] == {"host": "10.0.0.2", "port": 443}

    assert SECRET not in result.output


def test_init_flags_proxmox_host_on_a_node_that_powers_off(
    tmp_path: Path, project: Path, discovery: FakeDiscovery
) -> None:
    result = _init(project, _answers(tmp_path), "--always-on", "steady", "--include", "all")

    assert result.exit_code == 0, result.output
    assert "PROXMOX_HOST reaches the API through burst, which is not always-on" in result.output


def test_init_will_not_replace_files_unasked(project: Path, discovery: FakeDiscovery) -> None:
    (project / ".env").write_text("PROXMOX_TOKEN_SECRET=keep-me\n", encoding="utf-8")

    result = _run("init", "--dir", str(project), "--non-interactive")

    assert result.exit_code != 0
    assert "--force" in result.output
    assert (project / ".env").read_text(encoding="utf-8") == "PROXMOX_TOKEN_SECRET=keep-me\n"


def test_init_backs_up_what_it_replaces(
    tmp_path: Path, project: Path, discovery: FakeDiscovery
) -> None:
    (project / ".env").write_text("PROXMOX_TOKEN_SECRET=old\n", encoding="utf-8")
    (project / "config").mkdir()
    (project / "config" / "services.yaml").write_text("---\nversion: 1\n", encoding="utf-8")

    result = _init(project, _answers(tmp_path), "--include", "all")

    assert result.exit_code == 0, result.output
    [env_backup] = project.glob(".env.bak-*")
    [yaml_backup] = (project / "config").glob("services.bak-*.yaml")
    assert env_backup.read_text(encoding="utf-8") == "PROXMOX_TOKEN_SECRET=old\n"
    assert yaml_backup.read_text(encoding="utf-8") == "---\nversion: 1\n"


def test_init_writes_nothing_when_a_check_fails(
    tmp_path: Path, project: Path, discovery: FakeDiscovery
) -> None:
    discovery.fail_pbs = True

    result = _init(project, _answers(tmp_path), "--include", "all")

    assert result.exit_code != 0
    assert "PBS API check failed" in result.output
    assert list(project.iterdir()) == []


def test_init_no_verify_contacts_nothing(
    tmp_path: Path, project: Path, discovery: FakeDiscovery
) -> None:
    answers = _answers(tmp_path, PBS_DATASTORE="store1", PVE_BACKUP_STORAGE="pbs-lab")

    result = _init(project, answers, "--no-verify", "--always-on", "steady")

    assert result.exit_code == 0, result.output
    assert discovery.calls == 0
    registry = _yaml(project / "config" / "services.yaml")
    assert registry["nodes"] == [
        {"name": "steady", "always_on": True, "power_mgr_target": "steady", "notes": ""}
    ]
    assert registry["services"] == []


def test_init_treats_an_empty_env_as_a_clean_install(
    tmp_path: Path, project: Path, discovery: FakeDiscovery
) -> None:
    (project / ".env").write_text("", encoding="utf-8")  # what `touch .env` leaves

    result = _run(
        "init", "--dir", str(project), "--from", str(_answers(tmp_path)), "--non-interactive"
    )

    assert result.exit_code == 0, result.output
    assert read_env(project / ".env")["PROXMOX_TOKEN_SECRET"] == SECRET
    assert list(project.glob(".env.bak-*")) == []


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_show_masks_secrets(project: Path) -> None:
    (project / ".env").write_text(
        f"PROXMOX_TOKEN_SECRET={SECRET}\nPROXMOX_HOST=10.0.0.11\nMY_OWN_THING=abc123\n",
        encoding="utf-8",
    )

    result = _run("config", "show", "--dir", str(project))

    assert result.exit_code == 0, result.output
    assert "10.0.0.11" in result.output
    assert SECRET not in result.output
    assert "abc123" not in result.output  # unknown keys may be secrets too


def test_config_edit_changes_only_its_section(project: Path, discovery: FakeDiscovery) -> None:
    (project / ".env").write_text(
        f"PROXMOX_HOST=10.0.0.11\nPROXMOX_TOKEN_SECRET={SECRET}\n"
        "PROBE_PROXY_BASE_URL=https://10.0.0.2\n",
        encoding="utf-8",
    )

    # new proxy url, keep runner socket blank, no direct ssh, confirm the write
    result = _run(
        "config", "edit", "network", "--dir", str(project), input="https://10.0.0.3\n\nn\ny\n"
    )

    assert result.exit_code == 0, result.output
    env = read_env(project / ".env")
    assert env["PROBE_PROXY_BASE_URL"] == "https://10.0.0.3"
    assert env["PROXMOX_HOST"] == "10.0.0.11"
    assert env["PROXMOX_TOKEN_SECRET"] == SECRET
    assert len(list(project.glob(".env.bak-*"))) == 1
    assert "PROXMOX_PORT" not in result.output.split("Changes:")[1]  # defaults are not edits
    assert SECRET not in result.output


def test_config_edit_without_env_points_at_init(project: Path) -> None:
    result = _run("config", "edit", "pbs", "--dir", str(project))

    assert result.exit_code != 0
    assert "init" in result.output


# ---------------------------------------------------------------------------
# scaffold — the update command
# ---------------------------------------------------------------------------


def _seed_registry(path: Path) -> None:
    editor = RegistryEditor(path, fresh=True)
    editor.ensure_node("steady", always_on=True)
    editor.ensure_node("burst")
    editor.ensure_policy("daily-frequent")
    editor.upsert_service(
        "gateway", {"name": "gateway", "node": "steady", "guest_kind": "vm", "guest_id": 100}
    )
    # Renamed by hand and still on its old node: must be matched by VMID, not name.
    editor.upsert_service(
        "my-media",
        {"name": "Media (tuned)", "node": "steady", "guest_kind": "vm", "guest_id": 110},
    )
    editor.upsert_probe(
        "my-media",
        {"name": "http", "kind": "http", "required": True, "config": {"url": "https://m.test/"}},
    )
    editor.upsert_service(
        "retired", {"name": "retired", "node": "burst", "guest_kind": "vm", "guest_id": 999}
    )
    editor.save()


def test_scaffold_refuses_on_a_clean_install(discovery: FakeDiscovery) -> None:
    result = _run("scaffold", "--non-interactive")

    assert result.exit_code != 0
    assert "init" in result.output


def test_scaffold_updates_without_duplicating_or_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, discovery: FakeDiscovery
) -> None:
    monkeypatch.setenv("GATEWAY_SERVICE", "gateway")
    config_module.reset_settings_cache()
    path = tmp_path / "services.yaml"  # SERVICES_YAML_PATH, per conftest
    _seed_registry(path)
    discovery.cluster = ClusterStatus(
        nodes=[*CLUSTER.nodes, ClusterNode("fresh", online=True)], quorate=True
    )

    result = _run("scaffold", "--non-interactive", "--include", "all")

    assert result.exit_code == 0, result.output
    registry = _yaml(path)
    services = {s["slug"]: s for s in registry["services"]}
    assert set(services) == {"gateway", "my-media", "retired", "wiki"}  # media not re-added
    assert services["my-media"]["node"] == "burst"  # followed the move
    assert services["my-media"]["name"] == "Media (tuned)"  # hand-tuning kept
    assert services["my-media"]["probes"][0]["name"] == "http"  # probes kept
    assert services["wiki"]["depends_on"] == ["gateway"]
    assert {n["name"]: n["always_on"] for n in registry["nodes"]}["fresh"] is False
    assert "retired" in result.output.split("no longer in the cluster")[1]

    again = _run("scaffold", "--non-interactive", "--include", "all")
    assert again.exit_code == 0, again.output
    assert "already up to date" in again.output


def test_service_list_says_when_the_file_will_not_load(tmp_path: Path) -> None:
    (tmp_path / "services.yaml").write_text(
        "---\nversion: 1\nnodes: []\nbackup_policies: []\nservices:\n"
        "  - slug: orphan\n    name: Orphan\n    node: missing-node\n",
        encoding="utf-8",
    )

    result = _run("service", "list")

    assert "will NOT load" in result.output
    assert "missing-node" in result.output


def test_probe_add_suggests_the_proxy_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROBE_PROXY_BASE_URL", "https://10.0.0.2")
    config_module.reset_settings_cache()
    path = tmp_path / "services.yaml"
    _seed_registry(path)
    editor = RegistryEditor(path)
    editor.upsert_proxy_host("gateway", {"hostname": "gw.lab.test", "upstream": "http://10.0.0.5"})
    editor.save()

    result = _run("probe", "add", "gateway", "--non-interactive", "--kind", "http", "--name", "web")

    assert result.exit_code == 0, result.output
    [probe] = [p for s in _yaml(path)["services"] if s["slug"] == "gateway" for p in s["probes"]]
    assert probe["config"]["url"] == "https://10.0.0.2/"
    assert probe["config"]["host_header"] == "gw.lab.test"
