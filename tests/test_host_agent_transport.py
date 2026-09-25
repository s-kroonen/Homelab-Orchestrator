"""Host runner transport + the runner's allowlist.

Two things are load-bearing here and both are security properties, not
conveniences:

* the container cannot widen what the runner will do — the allowlist is
  enforced on the runner side, and a path cannot escape ``playbook_dir``;
* a refusal is a CONFIG error, not a service verdict. Mapping it to FAILED
  would let a misconfigured allowlist mark healthy services as corrupt.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.health.transports import host_agent
from orchestrator.health.transports.base import (
    TransportConfigError,
    TransportUnreachable,
)
from orchestrator.health.transports.host_agent import HostAgentTransport

# ---------------------------------------------------------------------------
# a fake socket the transport can talk to on any platform
# ---------------------------------------------------------------------------


class _FakeWriter:
    def __init__(self) -> None:
        self.sent = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.sent += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeReader:
    def __init__(self, reply: bytes) -> None:
        self._reply = reply

    async def readline(self) -> bytes:
        return self._reply


def _patch_socket(monkeypatch, reply: dict[str, Any] | bytes, capture: dict | None = None):
    raw = reply if isinstance(reply, bytes) else (json.dumps(reply) + "\n").encode()

    async def fake_open(socket_path: str):
        writer = _FakeWriter()
        if capture is not None:
            capture["writer"] = writer
            capture["socket_path"] = socket_path
        return _FakeReader(raw), writer

    monkeypatch.setattr(host_agent, "_open_connection", fake_open)


def _sent_request(capture: dict) -> dict[str, Any]:
    return json.loads(capture["writer"].sent.decode())


# ---------------------------------------------------------------------------
# request shaping
# ---------------------------------------------------------------------------


async def test_command_request_carries_argv(monkeypatch) -> None:
    capture: dict = {}
    _patch_socket(monkeypatch, {"rc": 0, "stdout": "active"}, capture)

    t = HostAgentTransport(socket_path="/run/x.sock", host="haos", action="command")
    result = await t.run(["systemctl", "is-active", "nginx"], timeout_s=30)

    req = _sent_request(capture)
    assert req["action"] == "command"
    assert req["host"] == "haos"
    assert req["argv"] == ["systemctl", "is-active", "nginx"]
    assert result.ok
    assert result.stdout == "active"


async def test_playbook_request_sends_a_name_not_a_path(monkeypatch) -> None:
    """The container must never choose a filesystem location — the runner
    resolves names against its own playbook_dir."""
    capture: dict = {}
    _patch_socket(monkeypatch, {"rc": 0}, capture)

    t = HostAgentTransport(
        socket_path="/run/x.sock",
        host="db-vm",
        action="playbook",
        playbook="check-mariadb.yml",
        extra_vars={"db": "prod"},
    )
    await t.run([], timeout_s=300)

    req = _sent_request(capture)
    assert req["playbook"] == "check-mariadb.yml"
    assert "/" not in req["playbook"]
    assert req["limit"] == "db-vm"
    assert req["extra_vars"] == {"db": "prod"}


async def test_playbook_action_without_a_name_is_a_config_error() -> None:
    t = HostAgentTransport(socket_path="/run/x.sock", action="playbook")
    with pytest.raises(TransportConfigError, match="playbook"):
        await t.run([], timeout_s=10)


def test_socket_path_is_required() -> None:
    with pytest.raises(TransportConfigError, match="socket"):
        HostAgentTransport(socket_path="")


# ---------------------------------------------------------------------------
# response mapping — the three-state contract
# ---------------------------------------------------------------------------


async def test_unreachable_target_raises_unreachable(monkeypatch) -> None:
    _patch_socket(monkeypatch, {"unreachable": True, "error": "ssh timed out"})
    t = HostAgentTransport(socket_path="/run/x.sock", host="haos")
    with pytest.raises(TransportUnreachable) as exc:
        await t.run(["true"], timeout_s=10)
    assert "says nothing about the service" in str(exc.value)


async def test_nonzero_rc_is_a_result_not_an_error(monkeypatch) -> None:
    """The check ran and said no — that IS a verdict, so it must come back as a
    CommandResult rather than an exception."""
    _patch_socket(monkeypatch, {"rc": 3, "stderr": "inactive"})
    t = HostAgentTransport(socket_path="/run/x.sock", host="haos")
    result = await t.run(["true"], timeout_s=10)
    assert result.exit_code == 3
    assert not result.ok


async def test_a_refusal_is_a_config_error_not_a_failure(monkeypatch) -> None:
    """A too-narrow allowlist is OUR problem. Treating it as FAILED would mark
    healthy services as corrupt because of a config mistake."""
    _patch_socket(
        monkeypatch,
        {"refused": True, "error": "playbook 'nope.yml' is not allowed"},
    )
    t = HostAgentTransport(socket_path="/run/x.sock", host="haos")
    with pytest.raises(TransportConfigError, match="refused"):
        await t.run(["true"], timeout_s=10)


async def test_missing_socket_explains_the_two_likely_causes(monkeypatch) -> None:
    async def boom(socket_path: str):
        raise FileNotFoundError(socket_path)

    monkeypatch.setattr(host_agent, "_open_connection", boom)
    t = HostAgentTransport(socket_path="/run/x.sock", host="haos")
    with pytest.raises(TransportUnreachable) as exc:
        await t.run(["true"], timeout_s=10)
    msg = str(exc.value)
    assert "running on the host" in msg
    assert "mounted into this container" in msg


async def test_permission_denied_points_at_the_group(monkeypatch) -> None:
    async def boom(socket_path: str):
        raise PermissionError(socket_path)

    monkeypatch.setattr(host_agent, "_open_connection", boom)
    t = HostAgentTransport(socket_path="/run/x.sock", host="haos")
    with pytest.raises(TransportUnreachable, match="group"):
        await t.run(["true"], timeout_s=10)


async def test_malformed_reply_is_unreachable(monkeypatch) -> None:
    _patch_socket(monkeypatch, b"not json\n")
    t = HostAgentTransport(socket_path="/run/x.sock", host="haos")
    with pytest.raises(TransportUnreachable, match="malformed"):
        await t.run(["true"], timeout_s=10)


async def test_empty_reply_is_unreachable(monkeypatch) -> None:
    _patch_socket(monkeypatch, b"")
    t = HostAgentTransport(socket_path="/run/x.sock", host="haos")
    with pytest.raises(TransportUnreachable, match="without replying"):
        await t.run(["true"], timeout_s=10)


async def test_hello_reports_what_the_runner_allows(monkeypatch) -> None:
    _patch_socket(monkeypatch, {"ok": True, "protocol": 1, "allow": {"ping": True}})
    t = HostAgentTransport(socket_path="/run/x.sock")
    info = await t.hello()
    assert info["ok"] is True
    assert info["allow"]["ping"] is True


# ---------------------------------------------------------------------------
# the runner's own allowlist — loaded as a module, no socket needed
# ---------------------------------------------------------------------------

_RUNNER = Path(__file__).parent.parent / "contrib" / "host-runner" / "orchestrator-host-runner"


def _load_runner():
    """Import the runner script, which has no .py extension by design."""
    import importlib.machinery
    import importlib.util

    spec = importlib.util.spec_from_loader(
        "host_runner_under_test",
        importlib.machinery.SourceFileLoader("host_runner_under_test", str(_RUNNER)),
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def runner():
    return _load_runner()


def _policy(runner, **overrides):
    cfg = {
        "socket_path": "/run/x.sock",
        "playbook_dir": "/etc/ansible/playbooks",
        "allow": {"ping": True, "playbooks": ["check-db.yml"], "hosts": ["haos"]},
    }
    cfg.update(overrides)
    return runner.Policy(cfg)


def test_runner_refuses_a_playbook_not_on_the_allowlist(runner) -> None:
    policy = _policy(runner)
    with pytest.raises(PermissionError, match="not allowed"):
        policy.resolve_playbook("something-else.yml")


def test_runner_refuses_a_host_outside_its_scope(runner) -> None:
    policy = _policy(runner)
    policy.check_host("haos")  # allowed
    with pytest.raises(PermissionError, match=r"allow\.hosts"):
        policy.check_host("some-other-box")


def test_empty_host_list_means_any_host(runner) -> None:
    policy = _policy(runner, allow={"hosts": []})
    policy.check_host("anything")  # must not raise


def test_runner_refuses_an_empty_host(runner) -> None:
    policy = _policy(runner, allow={"hosts": []})
    with pytest.raises(PermissionError):
        policy.check_host("")


def test_ad_hoc_commands_are_off_by_default(runner) -> None:
    """Because enabling them hands the container arbitrary remote execution."""
    policy = _policy(runner)
    with pytest.raises(PermissionError, match="disabled"):
        policy.check_command(["systemctl", "status"])


def test_command_allowlist_is_enforced_when_enabled(runner) -> None:
    policy = _policy(runner, allow={"command": True, "commands": ["systemctl"]})
    policy.check_command(["systemctl", "is-active", "x"])  # allowed
    with pytest.raises(PermissionError, match=r"allow\.commands"):
        policy.check_command(["rm", "-rf", "/"])


def test_timeouts_are_clamped_to_the_runner_maximum(runner) -> None:
    """A caller cannot pin the runner open indefinitely."""
    policy = _policy(runner, max_timeout_s=100)
    assert policy.clamp_timeout(999_999) == 100
    assert policy.clamp_timeout(-5) == 1
    assert policy.clamp_timeout("nonsense") == 60


def test_runner_parses_the_real_rc_from_ansible_json(runner) -> None:
    doc = json.dumps(
        {"plays": [{"tasks": [{"hosts": {"haos": {"rc": 3, "stdout": "", "stderr": "bad"}}}]}]}
    )
    parsed = runner.parse_ansible_json(doc)
    assert parsed["rc"] == 3


def test_runner_detects_unreachable_from_stats(runner) -> None:
    doc = json.dumps({"plays": [], "stats": {"haos": {"unreachable": 1}}})
    assert runner.parse_ansible_json(doc)["unreachable"] is True


async def test_runner_hello_lists_the_allowlist(runner) -> None:
    policy = _policy(runner)
    reply = await runner.handle_request(policy, {"action": "hello"})
    assert reply["ok"] is True
    assert reply["allow"]["playbooks"] == ["check-db.yml"]
    assert reply["allow"]["command"] is False


async def test_runner_rejects_an_unknown_action(runner) -> None:
    policy = _policy(runner)
    with pytest.raises(ValueError, match="unknown action"):
        await runner.handle_request(policy, {"action": "rm-rf"})


async def test_runner_refuses_command_action_when_disabled(runner) -> None:
    policy = _policy(runner)
    with pytest.raises(PermissionError):
        await runner.handle_request(policy, {"action": "command", "host": "haos", "argv": ["id"]})


def test_playbook_cannot_escape_the_playbook_dir(runner, tmp_path) -> None:
    """Belt-and-braces: names are allowlisted, but a traversal in the allowlist
    itself must not reach outside playbook_dir."""
    (tmp_path / "playbooks").mkdir()
    policy = _policy(
        runner,
        playbook_dir=str(tmp_path / "playbooks"),
        allow={"playbooks": ["../../etc/passwd"]},
    )
    with pytest.raises(PermissionError, match="outside playbook_dir"):
        policy.resolve_playbook("../../etc/passwd")


def test_asyncio_is_importable_without_unix_sockets(monkeypatch) -> None:
    """The transport must give a clear message on Windows rather than an
    AttributeError, since that is where development happens."""
    monkeypatch.delattr(asyncio, "open_unix_connection", raising=False)

    async def _go():
        with pytest.raises(TransportConfigError, match="unix sockets"):
            await host_agent._open_connection("/run/x.sock")

    asyncio.run(_go())
