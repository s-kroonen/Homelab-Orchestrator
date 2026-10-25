"""scripts/updater.sh against a fake docker: update, verify, roll back."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "updater.sh"
FAKE = Path(__file__).resolve().parent / "support" / "fake_docker.sh"

REF = "ghcr.io/example/orchestrator:dev"
OLD, NEW = "sha256:" + "a" * 64, "sha256:" + "b" * 64
BACKUP = "orchestrator-20261024-120000-pre-update.tar.gz"


class Rig:
    """A temp state dir, project dir and fake docker, plus helpers to drive them."""

    def __init__(self, tmp: Path) -> None:
        self.state = tmp / "data"
        self.docker = tmp / "docker-state"
        self.project = tmp / "project"
        self.bin = tmp / "bin"
        for d in (
            self.state / "update",
            self.state / "backups",
            self.docker,
            self.project,
            self.bin,
        ):
            d.mkdir(parents=True)
        (self.project / "docker-compose.yml").write_text("services: {}\n")
        (self.bin / "docker").write_text(FAKE.read_text())
        (self.bin / "docker").chmod(0o755)
        self.map = self.docker / "map"
        self.map.mkdir()
        # the running container: image OLD under tag REF
        self._set_ref(REF, OLD)
        (self.docker / "container.image").write_text(OLD)
        (self.docker / "container.ref").write_text(REF)
        (self.docker / "container.running").write_text("true")
        (self.docker / "container.health").write_text("healthy")
        self.sha(OLD, "aaaaaaa1111")
        self.sha(NEW, "bbbbbbb2222")
        self._backup()

    @staticmethod
    def _key(ref: str) -> str:
        return ref.replace("/", "_").replace(":", "_").replace("@", "_")

    def _set_ref(self, ref: str, image_id: str, *, after_pull: bool = False) -> None:
        target = self.docker / ("after_pull" if after_pull else "map")
        target.mkdir(exist_ok=True)
        (target / self._key(ref)).write_text(image_id)

    def sha(self, image_id: str, sha: str) -> None:
        (self.docker / "sha").mkdir(exist_ok=True)
        (self.docker / "sha" / image_id).write_text(sha)

    def _backup(self) -> None:
        path = self.state / "backups" / BACKUP
        with tarfile.open(path, "w:gz") as tar:
            for name, data in (
                ("orchestrator.db", b"SQLITE-OLD"),
                ("services.yaml", b"version: 1\n"),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    # -- scenario knobs
    def new_version(self, behavior: str = "healthy") -> None:
        self._set_ref(REF, NEW, after_pull=True)
        (self.docker / "behavior").mkdir(exist_ok=True)
        (self.docker / "behavior" / NEW).write_text(behavior)

    def flag(self, name: str) -> None:
        (self.docker / name).write_text("1")

    def request(self, **fields: str) -> None:
        lines = [f"{k.upper()}={v}" for k, v in fields.items()]
        (self.state / "update" / "request").write_text("\n".join(lines) + "\n")

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        full = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_DOCKER_STATE": str(self.docker),
            "FAKE_PROJECT_DIR": str(self.project),
            "STATE_DIR": str(self.state),
            "PROJECT_DIR": str(self.project),
            "HEALTH_TIMEOUT_S": "3",
            "SETTLE_S": "2",
            "POLL_S": "1",
            **env,
        }
        return subprocess.run(
            ["sh", str(SCRIPT), *(args or ("--once",))],
            env=full,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    # -- observations
    def status(self) -> dict:
        return json.loads((self.state / "update" / "status.json").read_text())

    def compose_calls(self) -> list[str]:
        f = self.docker / "compose_calls"
        return f.read_text().splitlines() if f.exists() else []

    def container_image(self) -> str:
        return (self.docker / "container.image").read_text().strip()

    def step_results(self) -> dict[str, str]:
        return {s["name"]: s["result"] for s in self.status()["steps"]}


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_a_healthy_new_version_replaces_the_old_one(rig: Rig) -> None:
    rig.new_version("healthy")
    rig.request(id="u1", action="update", by="storm", backup=BACKUP)

    result = rig.run()

    assert result.returncode == 0, result.stderr
    st = rig.status()
    assert st["state"] == "succeeded" and st["rolled_back"] is False
    assert st["from_sha"] == "aaaaaaa1111" and st["to_sha"] == "bbbbbbb2222"
    assert st["by"] == "storm" and st["id"] == "u1" and st["finished_at"]
    assert rig.container_image() == NEW
    (call,) = rig.compose_calls()
    assert f"ORCHESTRATOR_IMAGE={REF}" in call and "-p homelab" in call
    assert "--pull never" in call and "up -d --no-deps" in call
    # the old build stays available under a name that survives the tag moving
    assert (rig.map / "orchestrator-rollback_previous").read_text() == OLD
    assert not (rig.state / "restore-pending").exists()  # data untouched on success
    assert not (rig.state / "update" / "request").exists()  # request consumed
    assert (rig.state / "update" / "heartbeat").read_text().split()[1] == "1"


def test_it_recreates_the_container_from_the_compose_files_it_was_made_from(rig: Rig) -> None:
    (rig.project / "docker-compose.override.yml").write_text("services: {}\n")
    files = f"{rig.project}/docker-compose.yml,{rig.project}/docker-compose.override.yml"
    rig.new_version("healthy")
    rig.request(id="u0", action="update")

    rig.run(FAKE_CONFIG_FILES=files)

    (call,) = rig.compose_calls()
    assert (
        f"-f {rig.project}/docker-compose.yml -f {rig.project}/docker-compose.override.yml" in call
    )
    assert f"--project-directory {rig.project}" in call


def test_already_on_the_newest_build_does_nothing(rig: Rig) -> None:
    rig.request(id="u2", action="update")  # the pull brings the same image

    rig.run()

    st = rig.status()
    assert st["state"] == "succeeded" and "up to date" in st["message"]
    assert rig.compose_calls() == []


def test_an_unhealthy_new_version_is_rolled_back_with_its_data(rig: Rig) -> None:
    rig.new_version("unhealthy")
    rig.request(id="u3", action="update", backup=BACKUP)

    result = rig.run()

    assert result.returncode == 0, result.stderr
    st = rig.status()
    assert st["state"] == "rolled_back" and st["rolled_back"] is True
    assert "unhealthy" in st["reason"]
    assert rig.container_image() == OLD  # the previous version is running again
    new_call, old_call = rig.compose_calls()
    assert f"ORCHESTRATOR_IMAGE={REF}" in new_call
    # recreated under its ORIGINAL name, so the next update still pulls the right image
    assert f"ORCHESTRATOR_IMAGE={REF}" in old_call and "--force-recreate" in old_call
    assert (rig.map / rig._key(REF)).read_text() == OLD  # the local tag is back on the old build
    assert (rig.docker / "container.ref").read_text().strip() == REF
    # the backup taken before the update is staged for the old version to restore
    pending = rig.state / "restore-pending"
    assert (pending / "orchestrator.db").read_bytes() == b"SQLITE-OLD"
    assert (pending / "services.yaml").read_text() == "version: 1\n"
    marker = json.loads((pending / "pending.json").read_text())
    assert marker["backup"] == BACKUP and marker["has_registry"] is True
    results = rig.step_results()
    assert results["verify"] == "FAIL" and results["restore_data"] == "OK"
    assert "new version:" in (rig.state / "update" / "log").read_text()  # its last log lines


def test_a_crash_looping_new_version_is_rolled_back(rig: Rig) -> None:
    rig.new_version("flap")  # healthy for a moment, restarts keep climbing
    rig.request(id="u4", action="update", backup=BACKUP)

    rig.run()

    st = rig.status()
    assert st["state"] == "rolled_back" and "did not stay up" in st["reason"]
    assert rig.container_image() == OLD


def test_compose_failing_to_start_the_new_version_rolls_back(rig: Rig) -> None:
    rig.new_version("healthy")
    rig.flag("compose_fail_new")
    rig.request(id="u5", action="update", backup=BACKUP)

    rig.run()

    st = rig.status()
    assert st["state"] == "rolled_back"
    assert rig.container_image() == OLD


def test_a_failed_download_changes_nothing(rig: Rig) -> None:
    rig.new_version("healthy")
    rig.flag("pull_fail")
    rig.request(id="u6", action="update", backup=BACKUP)

    rig.run()

    st = rig.status()
    assert st["state"] == "failed" and "Could not download" in st["message"]
    assert rig.compose_calls() == [] and rig.container_image() == OLD
    assert not (rig.state / "restore-pending").exists()


def test_when_the_old_version_will_not_come_back_either_it_says_so(rig: Rig) -> None:
    rig.new_version("unhealthy")
    (rig.docker / "behavior").mkdir(exist_ok=True)
    (rig.docker / "behavior" / OLD).write_text("unhealthy")
    rig.request(id="u7", action="update", backup=BACKUP)

    rig.run()

    st = rig.status()
    assert st["state"] == "failed" and "not healthy either" in st["message"]
    assert st["rolled_back"] is True


def test_no_backup_named_means_no_restore_but_still_rolls_back(rig: Rig) -> None:
    rig.new_version("unhealthy")
    rig.request(id="u8", action="update")

    rig.run()

    assert rig.status()["state"] == "rolled_back"
    assert rig.step_results()["restore_data"] == "SKIP"
    assert not (rig.state / "restore-pending").exists()


def test_a_deliberate_rollback_goes_to_the_previous_build(rig: Rig) -> None:
    rig.new_version("healthy")
    rig.request(id="u9", action="update", backup=BACKUP)
    rig.run()
    assert rig.container_image() == NEW

    rig.request(id="u10", action="rollback", backup=BACKUP, by="storm")
    rig.run()

    st = rig.status()
    assert st["state"] == "rolled_back" and st["id"] == "u10"
    assert rig.container_image() == OLD
    assert (rig.state / "restore-pending" / "orchestrator.db").exists()


def test_a_rollback_with_nothing_to_go_back_to_fails_cleanly(rig: Rig) -> None:
    rig.request(id="u11", action="rollback")

    rig.run()

    st = rig.status()
    assert st["state"] == "failed" and "no previous build" in st["message"]


def test_a_digest_pinned_container_is_not_updated(rig: Rig) -> None:
    (rig.docker / "container.ref").write_text(f"ghcr.io/example/orchestrator@{NEW}")
    rig.request(id="u12", action="update")

    rig.run()

    st = rig.status()
    assert st["state"] == "failed" and "pinned to a digest" in st["message"]
    assert rig.compose_calls() == []


def test_the_request_can_only_choose_a_tag_of_the_same_image(rig: Rig) -> None:
    rig._set_ref("ghcr.io/example/orchestrator:main", NEW, after_pull=True)
    (rig.docker / "behavior").mkdir(exist_ok=True)
    (rig.docker / "behavior" / NEW).write_text("healthy")
    rig.request(id="u13", action="update", tag="main")

    rig.run()

    (call,) = rig.compose_calls()
    assert "ORCHESTRATOR_IMAGE=ghcr.io/example/orchestrator:main" in call
    pulls = [c for c in (rig.docker / "calls").read_text().splitlines() if c.startswith("pull")]
    assert pulls == ["pull ghcr.io/example/orchestrator:main"]


@pytest.mark.parametrize(
    "line",
    [
        "TAG=main; rm -rf /",
        "TAG=$(reboot)",
        "TAG=evil/other:image",
        "BACKUP=../../etc/passwd",
        "BY=a b",
        "ID=../x",
    ],
)
def test_hostile_request_fields_are_ignored_not_executed(rig: Rig, line: str) -> None:
    rig.new_version("healthy")
    (rig.state / "update" / "request").write_text(f"ACTION=update\n{line}\n")

    rig.run()

    st = rig.status()
    assert st["state"] == "succeeded"
    assert st["tag"] == "" and st["backup"] == ""  # the bad field was dropped
    pulls = [c for c in (rig.docker / "calls").read_text().splitlines() if c.startswith("pull")]
    assert pulls == [f"pull {REF}"]  # still the container's own image and tag


def test_a_request_without_an_action_is_refused(rig: Rig) -> None:
    (rig.state / "update" / "request").write_text("ID=x1\n")

    rig.run()

    st = rig.status()
    assert st["state"] == "failed" and "not understood" in st["message"]
    assert rig.compose_calls() == []


def test_the_command_line_form_updates_without_a_request_file(rig: Rig) -> None:
    rig.new_version("healthy")

    result = rig.run("--update", "--backup", BACKUP)

    assert result.returncode == 0, result.stderr
    assert rig.status()["state"] == "succeeded" and rig.container_image() == NEW


def test_old_rollback_images_are_pruned(rig: Rig) -> None:
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000", "20260104-000000"):
        (rig.map / f"orchestrator-rollback_{stamp}").write_text(OLD)
    rig.new_version("healthy")
    rig.request(id="u14", action="update")

    rig.run(KEEP_ROLLBACK_IMAGES="2")

    kept = sorted(
        p.name for p in rig.map.glob("orchestrator-rollback_*") if "previous" not in p.name
    )
    assert len(kept) == 2 and kept[-1] > "orchestrator-rollback_2026"  # the newest stay


# -- the published port ------------------------------------------------------------------------


def _ports(rig: Rig, before: str, after: str) -> None:
    (rig.docker / "port.before").write_text(before + "\n")
    (rig.docker / "port.after").write_text(after + "\n")


def test_an_update_that_moves_the_port_to_loopback_succeeds_but_warns(rig: Rig) -> None:
    rig.new_version("healthy")
    _ports(rig, "0.0.0.0:8080", "127.0.0.1:8080")
    rig.request(id="u1", action="update", by="storm", backup=BACKUP)

    result = rig.run()

    assert result.returncode == 0, result.stderr
    st = rig.status()
    assert (
        st["state"] == "succeeded"
    )  # rolling back would not help: the same compose brings it back
    (warn,) = [s for s in st["steps"] if s["name"] == "published_port"]
    assert warn["result"] == "WARN"
    assert "0.0.0.0:8080" in warn["detail"] and "127.0.0.1:8080" in warn["detail"]
    assert "ORCHESTRATOR_BIND_ADDR=0.0.0.0" in warn["detail"]


@pytest.mark.parametrize(
    ("before", "after"),
    [("0.0.0.0:8080", "0.0.0.0:8080"), ("127.0.0.1:8080", "127.0.0.1:8080"), ("", "")],
)
def test_an_unchanged_or_lan_port_is_not_mentioned(rig: Rig, before: str, after: str) -> None:
    rig.new_version("healthy")
    _ports(rig, before, after)
    rig.request(id="u1", action="update", by="storm", backup=BACKUP)

    assert rig.run().returncode == 0
    assert not [s for s in rig.status()["steps"] if s["name"] == "published_port"]
