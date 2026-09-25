"""Ansible ping probe — is this host reachable and answering?

The cheapest useful check when every guest already has an inventory entry. It
needs no hop configuration, no URL and no credentials in our config: the
inventory knows how to reach the host, so this probe only has to ask.

Executed by the **host runner**, not in this container: the inventory and keys
live on the host, and the container only asks for the check by name. See
docs/host_runner.md.

``ansible -m ping`` is not ICMP. It connects over the inventory's transport,
runs a tiny Python module on the far side and expects ``pong`` back — so a pass
means SSH worked, the interpreter ran, and the host is responsive. That is a
meaningfully stronger signal than a ping packet, and a meaningfully weaker one
than "the service is healthy": it says the *host* is alive, not the workload.

State mapping:

    pong                      ->  HEALTHY
    reachable but ping failed ->  FAILED   (module error, no interpreter, etc.)
    unreachable               ->  UNKNOWN  (indeterminate)

Pair it with a service-level probe. On its own it is a good *liveness* gate and
a poor *integrity* one — a host can answer ping while the database on it is
corrupt, and the whole point of the backup gate is to catch the latter.
"""

from __future__ import annotations

from collections.abc import Sequence

from orchestrator.db.models import Probe as ProbeRow
from orchestrator.domain.enums import HealthState, ProbeKind
from orchestrator.domain.schemas import ProbeResult
from orchestrator.health.probes.base import register_probe
from orchestrator.health.probes.commands import _CommandProbe
from orchestrator.health.transports.base import CommandResult
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


@register_probe
class AnsiblePingProbe(_CommandProbe):
    kind = ProbeKind.ANSIBLE_PING

    def _argv(self, row: ProbeRow) -> Sequence[str]:
        # The host runner performs `-m ping` based on the request's action; the
        # argv is unused for that path. `true` keeps this meaningful if someone
        # points the probe at a local or ssh transport instead.
        return ["true"]

    def _interpret(self, row: ProbeRow, result: CommandResult) -> ProbeResult:
        host = ((row.config.get("transport") or {}).get("host")) or "(inventory host)"
        if result.ok:
            return self._result(
                row,
                HealthState.HEALTHY,
                message=f"{host} answered",
                latency_ms=result.duration_ms,
                details={"host": host},
            )
        return self._result(
            row,
            HealthState.FAILED,
            message=(
                f"{host} was reached but did not answer cleanly "
                f"(exit {result.exit_code}): {result.tail()}"
            ),
            latency_ms=result.duration_ms,
            details={"host": host, "exit_code": result.exit_code},
        )
