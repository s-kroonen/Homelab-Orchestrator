"""Ping probe — ICMP echo to a host, run through a command transport.

``host`` blank means "this service's IP": the effective address (manual
``ip_address``, else what IP discovery found) is looked up when the probe runs,
so a re-discovered address is picked up without editing the probe. See
docs/service_ips.md.

    ping exits 0                        ->  HEALTHY  (latency = avg rtt)
    ping exits 1 (no reply)             ->  FAILED   (the host was asked, said nothing)
    ping exits 2 / cannot run / no IP   ->  UNKNOWN  (we could not ask)

The transport is optional and defaults to local (the container itself; Docker
grants NET_RAW by default). Point it at the host runner or ssh when the
orchestrator is not on the guests' network — see docs/probing_through_a_gateway.md.
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from orchestrator.db.models import Probe as ProbeRow
from orchestrator.domain.enums import HealthState, ProbeKind
from orchestrator.domain.schemas import PingProbeConfig, ProbeResult
from orchestrator.health.probes.base import Probe, register_probe
from orchestrator.health.transports.base import CommandTransport, TransportError

#: iputils: "rtt min/avg/max/mdev = 0.045/0.052/0.060/0.007 ms";
#: busybox: "round-trip min/avg/max = 0.045/0.052/0.060 ms".
_RTT = re.compile(r"(?:rtt|round-trip)[^=]*=\s*[\d.]+/([\d.]+)/")
_TIME = re.compile(r"time[=<]([\d.]+)\s*ms")
_HOST_OK = re.compile(r"^[A-Za-z0-9.:_\[\]%-]+$")


def parse_rtt_ms(output: str) -> float | None:
    """Average round-trip time in ms from ping's output, or None."""
    m = _RTT.search(output)
    if m:
        return float(m.group(1))
    times = [float(t) for t in _TIME.findall(output)]
    return sum(times) / len(times) if times else None


def resolve_host(row: ProbeRow, configured: str) -> str:
    """The configured host, or the service's effective IP when blank."""
    if configured.strip():
        return configured.strip()
    from orchestrator.ipdiscovery.discovery import resolve_service_ip

    return resolve_service_ip(row.service_id)


@register_probe
class PingProbe(Probe):
    kind = ProbeKind.PING
    #: The engine builds the transport from config.transport (default local).
    needs_transport = True

    async def run(
        self,
        row: ProbeRow,
        *,
        transport: CommandTransport | None = None,
    ) -> ProbeResult:
        try:
            cfg = PingProbeConfig.model_validate({**row.config, "kind": ProbeKind.PING})
        except ValidationError as exc:
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=f"probe config is invalid: {exc.errors()[0]['msg']}",
                details={"config_error": True},
            )

        host = resolve_host(row, cfg.host)
        if not host:
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=(
                    "this service has no IP yet — set its IP on the service page "
                    "(or `orchestrator-cli service update --ip`), or let IP discovery "
                    "find one (`orchestrator-cli ip discover`)"
                ),
                details={"no_ip": True, "indeterminate": True},
            )
        target = host.strip("[]")
        if target.startswith("-") or not _HOST_OK.match(target):
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=f"refusing to ping {host!r}: not a hostname or IP address",
                details={"config_error": True},
            )
        if transport is None:
            return self._result(
                row,
                HealthState.UNKNOWN,
                message="ping needs a transport but none was built",
                details={"config_error": True},
            )

        wait_s = max(1, min(5, row.timeout_s))
        argv = ["ping", "-c", str(cfg.count), "-W", str(wait_s), target]
        details: dict[str, object] = {
            "host": target,
            "host_from": "config" if cfg.host.strip() else "service_ip",
            "count": cfg.count,
        }
        try:
            result = await transport.run(argv, timeout_s=row.timeout_s)
        except TransportError as exc:
            return self._unknown_from_transport(row, exc)

        rtt = parse_rtt_ms(result.stdout)
        latency = round(rtt) if rtt is not None else result.duration_ms
        details["exit_code"] = result.exit_code
        if rtt is not None:
            details["rtt_avg_ms"] = rtt
        if result.ok:
            return self._result(
                row,
                HealthState.HEALTHY,
                message=f"{target} answered" + (f" in {rtt:.1f} ms" if rtt is not None else ""),
                latency_ms=latency,
                details=details,
            )
        if result.exit_code == 1:
            return self._result(
                row,
                HealthState.FAILED,
                message=f"{target} did not answer {cfg.count} ping(s)",
                latency_ms=latency,
                details=details,
            )
        details["output_tail"] = result.tail()
        details["indeterminate"] = True
        return self._result(
            row,
            HealthState.UNKNOWN,
            message=f"ping could not run (exit {result.exit_code}): {result.tail(200)}",
            latency_ms=latency,
            details=details,
        )
