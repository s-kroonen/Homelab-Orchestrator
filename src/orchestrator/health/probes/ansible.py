"""Ansible playbook probe.

The Pi is already the Ansible control host with an inventory, so for the more
involved checks a playbook is a better home than a shell string in YAML: it is
reviewable, testable on its own, and can use the module ecosystem.

This probe always runs on the *control host* (LocalTransport) — ansible reaches
out to the target itself using its own inventory and connection settings. That
is why it is a probe kind rather than a transport: ansible is not a way to run a
command, it is a thing that runs playbooks.

Exit 0 passes. Ansible's own exit codes distinguish the cases that matter:

    0  ok
    2  a task failed          -> FAILED   (the check ran and said no)
    4  unreachable host       -> UNKNOWN  (we learned nothing)
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from orchestrator.db.models import Probe as ProbeRow
from orchestrator.domain.enums import HealthState, ProbeKind
from orchestrator.domain.schemas import ProbeResult
from orchestrator.health.probes.base import Probe, register_probe
from orchestrator.health.transports.base import (
    CommandResult,
    CommandTransport,
    TransportError,
)
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

# ansible-playbook exit codes we treat specially.
_ANSIBLE_UNREACHABLE = 4
_ANSIBLE_TASK_FAILED = 2


@register_probe
class AnsiblePlaybookProbe(Probe):
    kind = ProbeKind.ANSIBLE_PLAYBOOK

    #: Runs on the control host, so it needs the local transport injected.
    needs_transport = True

    def _argv(self, row: ProbeRow) -> Sequence[str]:
        playbook = str(row.config["playbook"])
        argv: list[str] = [str(row.config.get("ansible_bin", "ansible-playbook")), playbook]

        inventory = row.config.get("inventory")
        if inventory:
            argv += ["-i", str(inventory)]

        limit = row.config.get("limit")
        if limit:
            argv += ["--limit", str(limit)]

        tags = row.config.get("tags")
        if tags:
            argv += ["--tags", ",".join(str(t) for t in tags)]

        extra_vars = row.config.get("extra_vars")
        if extra_vars:
            # JSON rather than key=value so nested structures and spaces survive.
            argv += ["--extra-vars", json.dumps(extra_vars)]

        if row.config.get("check_mode"):
            argv.append("--check")

        return argv

    async def run(
        self,
        row: ProbeRow,
        *,
        transport: CommandTransport | None = None,
    ) -> ProbeResult:
        if transport is None:
            return self._result(
                row,
                HealthState.UNKNOWN,
                message="ansible_playbook probe needs the local transport",
                details={"config_error": True},
            )

        try:
            argv = self._argv(row)
        except (KeyError, ValueError, TypeError) as exc:
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=f"probe config is invalid ({exc}) — `playbook` is required",
                details={"config_error": True},
            )

        playbook = str(row.config.get("playbook", ""))
        # Catch the most common operator mistake before spending a subprocess:
        # a playbook path that exists on the host but was never mounted into the
        # container. The error is otherwise a bare ansible "file not found".
        if playbook and not Path(playbook).exists():
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=(
                    f"playbook not found at {playbook!r}. In Docker this usually "
                    "means the playbook directory is not mounted into the container."
                ),
                details={"config_error": True, "playbook": playbook},
            )

        try:
            result = await transport.run(argv, timeout_s=row.timeout_s)
        except TransportError as exc:
            return self._unknown_from_transport(row, exc)

        return self._interpret(row, result)

    def _interpret(self, row: ProbeRow, result: CommandResult) -> ProbeResult:
        playbook = row.config.get("playbook")
        details: dict[str, object] = {
            "playbook": playbook,
            "exit_code": result.exit_code,
            "output_tail": result.tail(800),
        }

        if result.ok:
            return self._result(
                row,
                HealthState.HEALTHY,
                message=f"playbook {playbook} completed successfully",
                latency_ms=result.duration_ms,
                details=details,
            )

        if result.exit_code == _ANSIBLE_UNREACHABLE:
            # Ansible could not reach the host. Indeterminate, not a failure —
            # this is exactly the network-blip case that must not look like
            # corruption.
            details["indeterminate"] = True
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=f"playbook {playbook}: host unreachable (ansible exit 4)",
                latency_ms=result.duration_ms,
                details=details,
            )

        if result.exit_code == _ANSIBLE_TASK_FAILED:
            return self._result(
                row,
                HealthState.FAILED,
                message=f"playbook {playbook}: a task failed — {result.tail()}",
                latency_ms=result.duration_ms,
                details=details,
            )

        # Anything else (1 = error, 250 = unexpected) is ansible itself having a
        # problem rather than a verdict about the service.
        details["indeterminate"] = True
        return self._result(
            row,
            HealthState.UNKNOWN,
            message=(
                f"playbook {playbook}: ansible exited {result.exit_code}, which is an "
                f"ansible-level error rather than a check result — {result.tail()}"
            ),
            latency_ms=result.duration_ms,
            details=details,
        )
