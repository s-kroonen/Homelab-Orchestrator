"""Command probes — run something inside (or against) the guest via a transport.

All of these share one shape: build an argv, run it, map the exit code. What
differs is the argv and how the output is read.

**The DB checks run against a freshly produced dump, never live files** (spec
section 2). That is the point of the whole exercise: a live datafile is being
written to, so reading it proves nothing, while a dump that completes and
validates proves the engine could walk its own structures. It also means these
probes are slow — give them generous ``timeout_s``.
"""

from __future__ import annotations

from collections.abc import Sequence

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


class _CommandProbe(Probe):
    """Shared plumbing: needs a transport, runs one argv, maps the exit code."""

    needs_transport = True

    def _argv(self, row: ProbeRow) -> Sequence[str]:
        raise NotImplementedError

    def _interpret(self, row: ProbeRow, result: CommandResult) -> ProbeResult:
        """Default: exit 0 is healthy, anything else is a definitive failure."""
        if result.ok:
            return self._result(
                row,
                HealthState.HEALTHY,
                message="command exited 0",
                latency_ms=result.duration_ms,
                details={"exit_code": 0},
            )
        return self._result(
            row,
            HealthState.FAILED,
            message=f"command exited {result.exit_code}: {result.tail()}",
            latency_ms=result.duration_ms,
            details={"exit_code": result.exit_code, "output_tail": result.tail()},
        )

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
                message=(
                    f"probe kind {self.kind.value!r} needs a transport but none was "
                    "configured — add a `transport:` block to its config"
                ),
                details={"config_error": True},
            )
        try:
            argv = self._argv(row)
        except (KeyError, ValueError, TypeError) as exc:
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=f"probe config is invalid: {exc}",
                details={"config_error": True},
            )

        try:
            result = await transport.run(argv, timeout_s=row.timeout_s)
        except TransportError as exc:
            return self._unknown_from_transport(row, exc)

        return self._interpret(row, result)


@register_probe
class SystemdProbe(_CommandProbe):
    kind = ProbeKind.SYSTEMD

    def _argv(self, row: ProbeRow) -> Sequence[str]:
        unit = row.config["unit"]
        return ["systemctl", "is-active", "--quiet", str(unit)]

    def _interpret(self, row: ProbeRow, result: CommandResult) -> ProbeResult:
        unit = row.config.get("unit")
        if result.ok:
            return self._result(
                row,
                HealthState.HEALTHY,
                message=f"{unit} is active",
                latency_ms=result.duration_ms,
                details={"unit": unit},
            )
        # `is-active --quiet` exits 3 for inactive/failed. Either way the unit is
        # definitively not running, which is a real negative.
        return self._result(
            row,
            HealthState.FAILED,
            message=f"{unit} is not active (systemctl exited {result.exit_code})",
            latency_ms=result.duration_ms,
            details={"unit": unit, "exit_code": result.exit_code},
        )


@register_probe
class DockerProjectProbe(_CommandProbe):
    """Every container in a compose project must be running and, where it
    declares a healthcheck, healthy.

    Asks docker for one line per container rather than trusting `compose ps`
    exit status, which is 0 even when containers are down.
    """

    kind = ProbeKind.DOCKER_PROJECT

    def _argv(self, row: ProbeRow) -> Sequence[str]:
        project = str(row.config["project"])
        # Format gives us "name<TAB>state<TAB>status" per container.
        return [
            "docker",
            "ps",
            "--all",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.Names}}\t{{.State}}\t{{.Status}}",
        ]

    def _interpret(self, row: ProbeRow, result: CommandResult) -> ProbeResult:
        project = row.config.get("project")
        if not result.ok:
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=f"could not query docker: {result.tail()}",
                latency_ms=result.duration_ms,
                details={"indeterminate": True, "exit_code": result.exit_code},
            )

        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        if not lines:
            # Docker answered and the project has no containers at all. For a
            # project we were told to expect, that is a real negative.
            return self._result(
                row,
                HealthState.FAILED,
                message=f"compose project {project!r} has no containers",
                latency_ms=result.duration_ms,
                details={"project": project, "containers": []},
            )

        containers: list[dict[str, str]] = []
        bad: list[str] = []
        for line in lines:
            parts = line.split("\t")
            name = parts[0] if parts else "?"
            state = parts[1] if len(parts) > 1 else "?"
            status = parts[2] if len(parts) > 2 else ""
            containers.append({"name": name, "state": state, "status": status})
            if state != "running":
                bad.append(f"{name} is {state}")
            elif "unhealthy" in status.lower():
                bad.append(f"{name} is unhealthy")

        if bad:
            return self._result(
                row,
                HealthState.FAILED,
                message=f"compose project {project!r}: " + "; ".join(bad),
                latency_ms=result.duration_ms,
                details={"project": project, "containers": containers, "problems": bad},
            )

        return self._result(
            row,
            HealthState.HEALTHY,
            message=f"all {len(containers)} containers in {project!r} are running",
            latency_ms=result.duration_ms,
            details={"project": project, "containers": containers},
        )


@register_probe
class CustomScriptProbe(_CommandProbe):
    """Escape hatch: run an operator-supplied executable. Exit 0 passes.

    argv form only — no shell string — so a registry edit cannot smuggle in a
    pipeline. Operators who want shell semantics point `executable` at their own
    script.
    """

    kind = ProbeKind.CUSTOM_SCRIPT

    def _argv(self, row: ProbeRow) -> Sequence[str]:
        executable = str(row.config["executable"])
        args = [str(a) for a in row.config.get("args", [])]
        return [executable, *args]


@register_probe
class CommandProbe(_CommandProbe):
    """Escape hatch: run an arbitrary argv. Exit 0 passes.

    Distinct from CUSTOM_SCRIPT only in intent — this one is for one-off checks
    that do not warrant a script on the guest.
    """

    kind = ProbeKind.COMMAND

    def _argv(self, row: ProbeRow) -> Sequence[str]:
        argv = row.config["argv"]
        if isinstance(argv, str):
            raise ValueError(
                "`argv` must be a list of strings, not a shell string — "
                'e.g. ["sh", "-lc", "your command"] if you need a shell'
            )
        return [str(a) for a in argv]
