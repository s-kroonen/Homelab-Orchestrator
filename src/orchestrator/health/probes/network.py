"""Network probes — run from the orchestrator, no transport needed.

These cover the "simple curls to ports for web services" case. The HTTP probe is
the one most services will use, so its state mapping is worth being precise
about:

    connected, status in expect_status, body matches  ->  HEALTHY
    connected, status NOT in expect_status            ->  FAILED   (definitive)
    could not connect / timed out                     ->  UNKNOWN, marked unreachable
    TLS rejected                                      ->  UNKNOWN  (indeterminate)

A 500 is a real answer from a real service, so it is FAILED. A connection that never got
through is no answer at all; the probe marks it ``unreachable`` and the engine decides
what it means from the orchestrator's own network (``health/network.py``): with that
network up, the service is DOWN, or FAILED when its guest is running.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import time

import httpx

from orchestrator.db.models import Probe as ProbeRow
from orchestrator.domain.enums import HealthState, ProbeKind
from orchestrator.domain.schemas import HttpProbeConfig, ProbeResult, TcpProbeConfig
from orchestrator.health.probes.base import Probe, register_probe
from orchestrator.health.transports.base import CommandTransport
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


async def public_address(
    host: str, resolver: str, *, timeout: float = 5.0
) -> tuple[str | None, str]:
    """``(address, "")`` for ``host`` from the DNS-over-HTTPS server ``resolver`` (an IPv4
    address first, else IPv6), ``(None, "nxdomain")`` when it does not exist, ``(None,
    why)`` when the lookup failed."""
    base = resolver if "://" in resolver else f"https://{resolver}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for rtype in ("A", "AAAA"):
                r = await client.get(
                    base.rstrip("/") + "/dns-query",
                    params={"name": host, "type": rtype},
                    headers={"accept": "application/dns-json"},
                )
                r.raise_for_status()
                data = r.json()
                if data.get("Status") == 3:
                    return None, "nxdomain"
                want = 1 if rtype == "A" else 28
                for answer in data.get("Answer") or []:
                    if answer.get("type") == want and answer.get("data"):
                        return str(answer["data"]), ""
    except (httpx.HTTPError, ValueError) as exc:
        return None, str(exc) or type(exc).__name__
    return None, "no address in the answer"


def _tls_failure(exc: BaseException) -> bool:
    """Did the connection get as far as a TLS handshake that failed?"""
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, ssl.SSLError):
            return True
        seen = seen.__cause__ or seen.__context__
    text = str(exc).upper()
    return "SSL" in text or "CERTIFICATE" in text


@register_probe
class HttpProbe(Probe):
    kind = ProbeKind.HTTP

    async def run(
        self,
        row: ProbeRow,
        *,
        transport: CommandTransport | None = None,
    ) -> ProbeResult:
        try:
            cfg = HttpProbeConfig.model_validate({**row.config, "kind": ProbeKind.HTTP})
        except Exception as exc:
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=f"probe config is invalid: {exc}",
                details={"config_error": True},
            )

        # Host header + SNI override = curl --resolve. Lets us hit a reverse
        # proxy by its LOCAL ip while still routing (and validating TLS) as the
        # public hostname the proxy knows.
        headers = dict(cfg.headers)
        if cfg.host_header:
            headers["Host"] = cfg.host_header
        extensions: dict[str, object] = {}
        sni = cfg.effective_sni()
        if sni:
            extensions["sni_hostname"] = sni
        url = cfg.url

        started = time.monotonic()
        if cfg.resolve_via:
            # From outside: public DNS, then the public address with the real hostname.
            host = httpx.URL(cfg.url).host
            address, why = await public_address(host, cfg.resolve_via, timeout=row.timeout_s)
            if why == "nxdomain":
                return self._result(
                    row,
                    HealthState.UNKNOWN,
                    message=f"{host} has no public DNS record: nothing to check from outside",
                    details={"skipped": True, "skipped_because": "not_public", "url": cfg.url},
                )
            if address is None:
                return self._result(
                    row,
                    HealthState.UNKNOWN,
                    message=f"could not look {host} up via {cfg.resolve_via}: {why}",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    details={"indeterminate": True, "unreachable": True, "url": cfg.url},
                )
            url = str(httpx.URL(cfg.url).copy_with(host=address))
            headers["Host"] = host
            extensions["sni_hostname"] = host
            sni = host
        try:
            async with httpx.AsyncClient(
                verify=cfg.verify_tls,
                timeout=row.timeout_s,
                follow_redirects=True,
            ) as client:
                response = await client.request(
                    cfg.method,
                    url,
                    headers=headers,
                    extensions=extensions or None,
                )
        except httpx.TransportError as exc:
            # Never got an answer: DNS, connect, timeout - or a TLS handshake that failed,
            # which did reach something and is not "unreachable".
            via = f" (Host: {cfg.host_header})" if cfg.host_header else ""
            if cfg.resolve_via:
                via = f" (public address {httpx.URL(url).host})"
            tls = _tls_failure(exc)
            if tls and cfg.verify_tls:
                # A certificate that does not validate is an answer: visitors get it too.
                return self._result(
                    row,
                    HealthState.FAILED,
                    message=f"{cfg.url}{via}: the certificate was rejected: {exc}",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    details={"tls_error": True, "url": cfg.url},
                )
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=f"could not reach {cfg.url}{via}: {exc}",
                latency_ms=int((time.monotonic() - started) * 1000),
                details={
                    "indeterminate": True,
                    "unreachable": not tls,
                    "tls_error": tls,
                    "url": cfg.url,
                    "host_header": cfg.host_header,
                },
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        details: dict[str, object] = {
            "url": cfg.url,
            "status_code": response.status_code,
            "expected": cfg.expect_status,
        }
        if cfg.host_header:
            details["host_header"] = cfg.host_header
        if sni:
            details["sni_hostname"] = sni

        if not cfg.accepts(response.status_code):
            return self._result(
                row,
                HealthState.FAILED,
                message=(
                    f"{cfg.url} returned {response.status_code}, "
                    f"expected one of {cfg.expect_status}"
                ),
                latency_ms=latency_ms,
                details=details,
            )

        if cfg.expect_body_contains and cfg.expect_body_contains not in response.text:
            details["expect_body_contains"] = cfg.expect_body_contains
            return self._result(
                row,
                HealthState.FAILED,
                message=(
                    f"{cfg.url} returned {response.status_code} but the body did not "
                    f"contain {cfg.expect_body_contains!r}"
                ),
                latency_ms=latency_ms,
                details=details,
            )

        return self._result(
            row,
            HealthState.HEALTHY,
            message=f"{cfg.url} returned {response.status_code}",
            latency_ms=latency_ms,
            details=details,
        )


@register_probe
class TcpProbe(Probe):
    """Bare socket connect — for stacks with no HTTP surface (game servers,
    database ports, mail)."""

    kind = ProbeKind.TCP

    async def run(
        self,
        row: ProbeRow,
        *,
        transport: CommandTransport | None = None,
    ) -> ProbeResult:
        try:
            cfg = TcpProbeConfig.model_validate({**row.config, "kind": ProbeKind.TCP})
        except Exception as exc:
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=f"probe config is invalid: {exc}",
                details={"config_error": True},
            )

        started = time.monotonic()
        writer = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(cfg.host, cfg.port), timeout=row.timeout_s
            )
        except TimeoutError:
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=f"connect to {cfg.host}:{cfg.port} timed out after {row.timeout_s}s",
                latency_ms=int((time.monotonic() - started) * 1000),
                details={
                    "indeterminate": True,
                    "unreachable": True,
                    "host": cfg.host,
                    "port": cfg.port,
                },
            )
        except ConnectionRefusedError:
            # A refusal IS an answer: something is listening on the host stack and
            # actively said no. For a port that should be open, that is a real
            # negative rather than an unknown.
            return self._result(
                row,
                HealthState.FAILED,
                message=f"connection to {cfg.host}:{cfg.port} was refused",
                latency_ms=int((time.monotonic() - started) * 1000),
                details={"host": cfg.host, "port": cfg.port, "refused": True},
            )
        except OSError as exc:
            # DNS failure, no route, etc. — we never got to the service.
            return self._result(
                row,
                HealthState.UNKNOWN,
                message=f"could not connect to {cfg.host}:{cfg.port}: {exc}",
                latency_ms=int((time.monotonic() - started) * 1000),
                details={
                    "indeterminate": True,
                    "unreachable": True,
                    "host": cfg.host,
                    "port": cfg.port,
                },
            )
        finally:
            if writer is not None:
                writer.close()
                # Closing is best-effort cleanup; a failure here says nothing
                # about the service's health.
                with contextlib.suppress(OSError):
                    await writer.wait_closed()

        return self._result(
            row,
            HealthState.HEALTHY,
            message=f"{cfg.host}:{cfg.port} accepted a connection",
            latency_ms=int((time.monotonic() - started) * 1000),
            details={"host": cfg.host, "port": cfg.port},
        )
