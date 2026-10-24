"""scripts/install.sh: one command to install or update the compose files and the updater."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "scripts" / "install.sh"
SUPPORT = Path(__file__).resolve().parent / "support"
FILES = [
    "docker-compose.yml",
    "docker-compose.updater.yml",
    "scripts/updater.sh",
    "scripts/install.sh",
]


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.target = tmp / "orchestrator"
        self.remote = tmp / "remote"  # what "GitHub" serves
        self.bin = tmp / "bin"
        self.state = tmp / "docker-state"
        self.curl_log = tmp / "curl.log"
        for d in (self.target, self.bin, self.state):
            d.mkdir()
        for f in FILES:
            (self.remote / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / f, self.remote / f)
        shutil.copy(SUPPORT / "fake_curl.sh", self.bin / "curl")
        shutil.copy(SUPPORT / "fake_docker_install.sh", self.bin / "docker")
        for tool in ("curl", "docker"):
            (self.bin / tool).chmod(0o755)

    def env_file(self, text: str) -> None:
        (self.target / ".env").write_text(text)

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        full = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_REPO_ROOT": str(self.remote),
            "FAKE_DOCKER_STATE": str(self.state),
            "FAKE_CURL_LOG": str(self.curl_log),
            "INSTALL_POLL_S": "0",
            "INSTALL_POLL_MAX": "5",
            **env,
        }
        return subprocess.run(
            ["sh", str(INSTALL), "--dir", str(self.target), *args],
            env=full,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self) -> list[str]:
        f = self.state / "calls"
        return f.read_text().splitlines() if f.exists() else []

    def refs(self) -> set[str]:
        return {u.split("/")[5] for u in self.curl_log.read_text().split()}

    def marker(self) -> dict[str, str]:
        out = {}
        for line in (self.target / ".orchestrator-install").read_text().splitlines():
            digest, name = line.split("  ", 1)
            out[name] = digest
        return out


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_a_fresh_directory_gets_the_files_and_setup_instructions(rig: Rig) -> None:
    result = rig.run()

    assert result.returncode == 0, result.stderr
    for f in FILES:
        assert (rig.target / f).read_text() == (REPO / f).read_text()
    assert (rig.target / "config").is_dir() and (rig.target / ".env").exists()
    assert set(rig.marker()) == set(FILES)
    assert "docker compose run --rm setup init" in result.stdout
    # an empty .env is a fresh install: nothing is started until it is configured
    assert not any(" up " in c or c.endswith(" pull orchestrator") for c in rig.calls())


def test_a_configured_install_starts_the_updater_and_the_orchestrator(rig: Rig) -> None:
    rig.env_file("ORCHESTRATOR_IMAGE=ghcr.io/s-kroonen/homelab-orchestrator:dev\n")

    result = rig.run()

    assert result.returncode == 0, result.stderr
    calls = rig.calls()
    both = "compose -f docker-compose.yml -f docker-compose.updater.yml"
    assert f"{both} up -d updater" in calls
    assert "compose pull orchestrator" in calls and "compose up -d orchestrator" in calls
    assert rig.refs() == {"dev"}  # taken from the image tag in .env
    assert (rig.target / ".env").read_text().startswith("ORCHESTRATOR_IMAGE")  # never touched


def test_running_it_again_changes_nothing_when_nothing_changed(rig: Rig) -> None:
    rig.env_file("ORCHESTRATOR_IMAGE=x/y:main\n")
    rig.run()

    again = rig.run()

    assert again.returncode == 0
    assert again.stdout.count("unchanged") == len(FILES)
    assert not list(rig.target.glob("**/*.bak")) and not list(rig.target.glob("**/*.new"))


def test_a_file_you_edited_is_kept_and_the_new_one_is_written_beside_it(rig: Rig) -> None:
    rig.env_file("ORCHESTRATOR_IMAGE=x/y:main\n")
    rig.run()
    mine = rig.target / "docker-compose.yml"
    mine.write_text(mine.read_text() + "\n# my own change\n")
    (rig.remote / "docker-compose.yml").write_text("services: {}  # upstream changed\n")

    result = rig.run()

    assert result.returncode == 0
    assert "my own change" in mine.read_text()
    assert (rig.target / "docker-compose.yml.new").read_text().startswith("services: {}")
    assert "left alone" in result.stderr and "diff docker-compose.yml" in result.stderr


def test_a_file_we_wrote_and_you_did_not_touch_is_updated_with_a_backup(rig: Rig) -> None:
    rig.env_file("ORCHESTRATOR_IMAGE=x/y:main\n")
    rig.run()
    old = (rig.target / "scripts/updater.sh").read_text()
    (rig.remote / "scripts/updater.sh").write_text(old + "\n# a newer updater\n")

    result = rig.run()

    assert "updated    scripts/updater.sh" in result.stdout
    assert "a newer updater" in (rig.target / "scripts/updater.sh").read_text()
    assert (rig.target / "scripts/updater.sh.bak").read_text() == old


def test_with_the_updater_running_the_update_goes_through_it(rig: Rig) -> None:
    rig.env_file("ORCHESTRATOR_IMAGE=x/y:dev\n")

    result = rig.run(FAKE_HAS_ORCHESTRATOR="1", FAKE_UPDATER_RUNNING="1")

    assert result.returncode == 0, result.stderr
    calls = rig.calls()
    assert "exec orchestrator orchestrator-cli system update" in calls
    assert "compose pull orchestrator" not in calls  # the updater pulls (and can roll back)
    assert "Done." in result.stdout and "succeeded" in result.stdout
    # the updater was refreshed BEFORE the update was requested
    assert calls.index(next(c for c in calls if c.endswith("up -d updater"))) < calls.index(
        "exec orchestrator orchestrator-cli system update"
    )


def test_a_rolled_back_update_makes_the_script_fail_loudly(rig: Rig) -> None:
    rig.env_file("ORCHESTRATOR_IMAGE=x/y:dev\n")

    result = rig.run(FAKE_HAS_ORCHESTRATOR="1", FAKE_UPDATER_RUNNING="1", FAKE_RESULT="rolled_back")

    assert result.returncode == 1
    assert "previous version was put back" in result.stderr


def test_an_older_image_without_the_updater_support_gets_a_plain_update(rig: Rig) -> None:
    rig.env_file("ORCHESTRATOR_IMAGE=x/y:dev\n")

    result = rig.run(FAKE_HAS_ORCHESTRATOR="1", FAKE_UPDATER_RUNNING="1", FAKE_OLD_IMAGE="1")

    assert result.returncode == 0, result.stderr
    calls = rig.calls()
    assert "compose pull orchestrator" in calls and "compose up -d orchestrator" in calls
    assert "exec orchestrator orchestrator-cli system update" not in calls
    assert "no rollback on this path" in result.stdout


def test_no_updater_flag_skips_it(rig: Rig) -> None:
    rig.env_file("ORCHESTRATOR_IMAGE=x/y:dev\n")

    rig.run("--no-updater", FAKE_HAS_ORCHESTRATOR="1", FAKE_UPDATER_RUNNING="1")

    calls = rig.calls()
    assert not any("up -d updater" in c for c in calls)
    assert "compose pull orchestrator" in calls


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("ghcr.io/s-kroonen/homelab-orchestrator:dev", "dev"),
        ("ghcr.io/s-kroonen/homelab-orchestrator:main", "main"),
        ("ghcr.io/s-kroonen/homelab-orchestrator:v1.2.3", "v1.2.3"),
        ("ghcr.io/s-kroonen/homelab-orchestrator:sha-abc1234", "main"),
        ("registry.local:5000/orchestrator", "main"),
    ],
)
def test_the_files_come_from_the_ref_the_image_follows(rig: Rig, image: str, expected: str) -> None:
    rig.env_file(f"ORCHESTRATOR_IMAGE={image}\n")
    # the fake serves any ref
    rig.run("--no-start")
    assert rig.refs() == {expected}


def test_ref_and_repo_can_be_chosen(rig: Rig) -> None:
    rig.run("--no-start", "--ref", "abc1234", "--repo", "someone/fork")
    urls = rig.curl_log.read_text().split()
    assert all(
        u.startswith("https://raw.githubusercontent.com/someone/fork/abc1234/") for u in urls
    )


def test_a_failed_download_installs_nothing(rig: Rig) -> None:
    (rig.remote / "scripts/updater.sh").unlink()

    result = rig.run()

    assert result.returncode == 1 and "could not download" in result.stderr
    assert not (rig.target / "docker-compose.yml").exists()


def test_no_start_only_places_the_files(rig: Rig) -> None:
    rig.env_file("ORCHESTRATOR_IMAGE=x/y:dev\n")

    result = rig.run("--no-start")

    assert result.returncode == 0 and "not starting anything" in result.stdout
    assert all(c.startswith("compose version") for c in rig.calls())
