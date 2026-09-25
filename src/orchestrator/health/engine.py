"""Health / scan engine — runs a service's probes and returns a three-state verdict.

Aggregation rule (spec section 4), applied over REQUIRED probes only:

    all required HEALTHY        -> HEALTHY   the only state that backs up
    any required FAILED         -> FAILED    skip backup, restore candidate
    otherwise                   -> UNKNOWN   skip backup, alert only

Two deliberate choices in that rule:

* **FAILED beats UNKNOWN.** If one probe says "definitely broken" and another
  says "could not tell", the service is broken. The definite signal wins.
* **No required probes at all -> UNKNOWN, not HEALTHY.** A service nobody wrote a
  check for has not been verified, and treating unverified as healthy would open
  the gate on exactly the services least understood. This is the single most
  important line in the file: it makes forgetting to configure a probe fail
  closed rather than silently permissive.

Non-required probes are recorded for the dashboard and ignored by the verdict.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, select

from orchestrator.config import Settings, get_settings
from orchestrator.db.models import Probe as ProbeRow
from orchestrator.db.models import Service, ServiceState
from orchestrator.domain.enums import HealthState
from orchestrator.domain.schemas import ProbeResult, ServiceVerdict
from orchestrator.health.probes import get_probe_class
from orchestrator.health.transports.base import (
    CommandTransport,
    TransportConfigError,
    TransportSpec,
)
from orchestrator.health.transports.dry_run import DryRunTransport
from orchestrator.health.transports.local import LocalTransport
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


def _spec_from_config(config: dict[str, object]) -> TransportSpec:
    """Read the optional ``transport:`` block out of a probe's config."""
    raw = config.get("transport")
    if not isinstance(raw, dict):
        # No block: default to local. Correct for ansible_playbook (which runs on
        # the control host) and for scripts living on the Pi.
        return TransportSpec(type="local")
    known = {"type", "host", "user", "port"}
    return TransportSpec(
        type=str(raw.get("type", "local")),
        host=str(raw["host"]) if raw.get("host") else None,
        user=str(raw["user"]) if raw.get("user") else None,
        port=int(raw["port"]) if raw.get("port") else None,  # type: ignore[arg-type]
        extra={k: v for k, v in raw.items() if k not in known},
    )


class TransportFactory:
    """Builds transports from a probe's declarative ``transport:`` block.

    Injectable so tests and DRY_RUN can substitute a recording transport without
    the probes knowing.
    """

    def __init__(self, settings: Settings | None = None, *, force_dry_run: bool = False) -> None:
        self._settings = settings or get_settings()
        self._force_dry_run = force_dry_run or self._settings.dry_run
        self._dry_run_transport = DryRunTransport()

    def build(self, spec: TransportSpec) -> CommandTransport:
        if self._force_dry_run:
            return self._dry_run_transport

        kind = spec.type.lower()
        if kind in {"local", "control_host", ""}:
            return LocalTransport()
        if kind == "ssh":
            # Imported lazily so a deployment that never uses SSH probes does not
            # pay for asyncssh at startup.
            from orchestrator.health.transports.ssh import SshTransport

            if not spec.host:
                raise TransportConfigError(
                    "ssh transport requires `host` in the probe's transport block"
                )
            extra = spec.extra
            return SshTransport(
                host=spec.host,
                user=spec.user or "root",
                port=spec.port or 22,
                key_path=str(extra.get("key_path") or self._settings.ssh_key_path or "") or None,
                known_hosts_path=str(
                    extra.get("known_hosts_path") or self._settings.ssh_known_hosts_path or ""
                )
                or None,
                verify_host_key=bool(
                    extra.get("verify_host_key", self._settings.ssh_verify_host_key)
                ),
                # "" in a probe's block explicitly means "no jump", which is how
                # you reach a host that IS directly routable.
                jump_host=(
                    str(extra["jump_host"])
                    if "jump_host" in extra
                    else self._settings.ssh_jump_host
                )
                or None,
                jump_user=str(extra.get("jump_user") or self._settings.ssh_jump_user) or None,
                jump_port=int(extra.get("jump_port") or self._settings.ssh_jump_port),
            )
        if kind in {"host_agent", "ansible", "host_runner"}:
            # "ansible" is accepted as an alias because that is how an operator
            # thinks about it — but execution happens on the HOST, via the
            # runner. Nothing ansible-related lives in this container.
            from orchestrator.health.transports.host_agent import HostAgentTransport

            extra = spec.extra
            return HostAgentTransport(
                socket_path=str(
                    extra.get("socket_path") or self._settings.host_runner_socket or ""
                ),
                host=spec.host,
                action=str(extra.get("action") or "command"),
                playbook=str(extra["playbook"]) if extra.get("playbook") else None,
                extra_vars=dict(extra.get("extra_vars") or {}),  # type: ignore[arg-type]
            )
        raise TransportConfigError(
            f"unknown transport type {spec.type!r}. "
            f"Known types: local, ssh, host_agent (alias: ansible)"
        )


class HealthEngine:
    """Scans services and persists the verdict."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transports = transport_factory or TransportFactory(self._settings)

    async def scan(
        self,
        session: Session,
        service: Service,
        *,
        _cache: dict[str, ServiceVerdict] | None = None,
        _visiting: frozenset[str] | None = None,
    ) -> ServiceVerdict:
        """Run every probe for ``service`` and return the aggregated verdict.

        Dependencies are scanned first. If one is not HEALTHY, this service
        short-circuits to UNKNOWN without running its own probes — they would be
        guaranteed to time out, and the resulting failures would describe the
        wrong problem.
        """
        cache = {} if _cache is None else _cache
        visiting = frozenset() if _visiting is None else _visiting

        blocker = await self._check_dependencies(session, service, cache, visiting)
        if blocker is not None:
            self._persist(session, service, blocker)
            log.info(
                "health.scan.blocked",
                service=service.slug,
                verdict=blocker.state.value,
                reason=blocker.reason,
            )
            cache[service.slug] = blocker
            return blocker

        rows = session.exec(
            select(ProbeRow)
            .where(ProbeRow.service_id == service.id)
            .order_by(ProbeRow.order, ProbeRow.name)
        ).all()

        results: list[ProbeResult] = []
        for row in rows:
            results.append(await self._run_one(row))

        verdict = self.aggregate(rows, results)
        cache[service.slug] = verdict
        self._persist(session, service, verdict)
        log.info(
            "health.scan.complete",
            service=service.slug,
            verdict=verdict.state.value,
            reason=verdict.reason,
            probes=len(results),
        )
        return verdict

    async def _check_dependencies(
        self,
        session: Session,
        service: Service,
        cache: dict[str, ServiceVerdict],
        visiting: frozenset[str],
    ) -> ServiceVerdict | None:
        """Scan this service's dependencies. Return a blocking verdict, or None.

        The blocking verdict is always UNKNOWN, never FAILED — even when the
        dependency itself is FAILED. A broken gateway is definitive information
        about the *gateway*; about the service behind it we have learned nothing,
        and marking it FAILED would make it a restore candidate on the strength
        of someone else's outage.
        """
        deps = list(service.depends_on or [])
        if not deps:
            return None

        for dep_slug in deps:
            if dep_slug in visiting:
                # validate_references catches cycles in the file; this guards the
                # DB path (a hand-edited row, or config drifted at runtime).
                return ServiceVerdict(
                    state=HealthState.UNKNOWN,
                    reason=(
                        f"dependency cycle detected at {dep_slug!r} — cannot establish "
                        f"health for {service.slug!r}"
                    ),
                    probe_results=[],
                )

            verdict = cache.get(dep_slug)
            if verdict is None:
                dep = session.exec(select(Service).where(Service.slug == dep_slug)).one_or_none()
                if dep is None:
                    return ServiceVerdict(
                        state=HealthState.UNKNOWN,
                        reason=(
                            f"depends on {dep_slug!r}, which is not in the registry — "
                            f"cannot establish health"
                        ),
                        probe_results=[],
                    )
                verdict = await self.scan(
                    session, dep, _cache=cache, _visiting=visiting | {service.slug}
                )

            if verdict.state is not HealthState.HEALTHY:
                return ServiceVerdict(
                    state=HealthState.UNKNOWN,
                    reason=(
                        f"blocked: dependency {dep_slug!r} is {verdict.state.value.upper()} "
                        f"({verdict.reason}). This service was not probed, so this is "
                        f"NOT evidence about {service.slug!r} itself."
                    ),
                    probe_results=[],
                )

        return None

    async def _run_one(self, row: ProbeRow) -> ProbeResult:
        probe_cls = get_probe_class(row.kind)
        if probe_cls is None:
            return ProbeResult(
                probe_name=row.name,
                kind=row.kind,
                state=HealthState.UNKNOWN,
                message=(f"no implementation registered for probe kind {row.kind.value!r}"),
                details={"config_error": True},
            )

        probe = probe_cls()
        transport: CommandTransport | None = None
        if probe.needs_transport:
            try:
                transport = self._transports.build(_spec_from_config(row.config))
            except TransportConfigError as exc:
                return ProbeResult(
                    probe_name=row.name,
                    kind=row.kind,
                    state=HealthState.UNKNOWN,
                    message=str(exc),
                    details={"config_error": True},
                )

        try:
            return await probe.run(row, transport=transport)
        except Exception as exc:
            # A probe implementation blowing up is our bug, not a service verdict.
            # UNKNOWN keeps the gate closed without libelling the service.
            log.error(
                "health.probe.crashed",
                probe=row.name,
                kind=row.kind.value,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return ProbeResult(
                probe_name=row.name,
                kind=row.kind,
                state=HealthState.UNKNOWN,
                message=f"probe implementation raised {type(exc).__name__}: {exc}",
                details={"probe_bug": True, "indeterminate": True},
            )

    def aggregate(
        self, rows: list[ProbeRow] | tuple[ProbeRow, ...], results: list[ProbeResult]
    ) -> ServiceVerdict:
        """Combine probe results into one verdict. See the module docstring."""
        required_by_name = {r.name for r in rows if r.required}
        required = [r for r in results if r.probe_name in required_by_name]

        if not required:
            return ServiceVerdict(
                state=HealthState.UNKNOWN,
                reason=(
                    "no required probes are configured, so nothing has been verified — "
                    "add at least one probe with required: true before this service "
                    "can be backed up"
                ),
                probe_results=results,
            )

        failed = [r for r in required if r.state is HealthState.FAILED]
        if failed:
            names = ", ".join(f"{r.probe_name} ({r.message})" for r in failed)
            return ServiceVerdict(
                state=HealthState.FAILED,
                reason=f"{len(failed)} required probe(s) failed: {names}",
                probe_results=results,
            )

        unknown = [r for r in required if r.state is HealthState.UNKNOWN]
        if unknown:
            names = ", ".join(f"{r.probe_name} ({r.message})" for r in unknown)
            return ServiceVerdict(
                state=HealthState.UNKNOWN,
                reason=f"{len(unknown)} required probe(s) were indeterminate: {names}",
                probe_results=results,
            )

        return ServiceVerdict(
            state=HealthState.HEALTHY,
            reason=f"all {len(required)} required probe(s) passed",
            probe_results=results,
        )

    def _persist(self, session: Session, service: Service, verdict: ServiceVerdict) -> None:
        """Write the verdict to ServiceState so the dashboard and the gate agree."""
        state = session.get(ServiceState, service.id)
        if state is None:
            state = ServiceState(service_id=service.id)  # type: ignore[arg-type]
            session.add(state)
        state.last_verdict = verdict.state
        state.last_verdict_at = datetime.now(UTC)
        state.last_probe_results = [r.model_dump(mode="json") for r in verdict.probe_results]
        state.notes = verdict.reason
        session.add(state)
        session.commit()
