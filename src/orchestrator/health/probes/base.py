"""Probe base class and the registry that maps ``ProbeKind`` -> implementation.

Adding a probe kind is two steps and no orchestrator-wide change:

1. Add the value to :class:`orchestrator.domain.enums.ProbeKind`.
2. Subclass :class:`Probe` here (or in a sibling module) and decorate it with
   ``@register_probe``.

Probes never raise for a negative result. They translate every outcome into a
:class:`ProbeResult` carrying one of the three health states, because the
distinction between "checked, bad" and "could not check" is load-bearing:

    FAILED   the check ran and the answer was no      -> restore candidate
    UNKNOWN  the check could not run or was ambiguous -> alert only
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from orchestrator.db.models import Probe as ProbeRow
from orchestrator.domain.enums import HealthState, ProbeKind
from orchestrator.domain.schemas import ProbeResult
from orchestrator.health.transports.base import (
    CommandTransport,
    TransportError,
    TransportUnreachable,
)


class Probe(ABC):
    """One health check."""

    kind: ClassVar[ProbeKind]

    #: Command probes need a transport; network probes reach out themselves.
    needs_transport: ClassVar[bool] = False

    @abstractmethod
    async def run(
        self,
        row: ProbeRow,
        *,
        transport: CommandTransport | None = None,
    ) -> ProbeResult: ...

    # -- helpers shared by implementations ---------------------------------

    def _result(
        self,
        row: ProbeRow,
        state: HealthState,
        *,
        message: str,
        latency_ms: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> ProbeResult:
        return ProbeResult(
            probe_name=row.name,
            kind=self.kind,
            state=state,
            latency_ms=latency_ms,
            message=message,
            details=details or {},
        )

    def _unknown_from_transport(self, row: ProbeRow, exc: TransportError) -> ProbeResult:
        """Map any transport-level problem onto UNKNOWN.

        Never FAILED: an unreachable host, a rejected key or a missing binary
        tells us nothing about whether the service is healthy, and FAILED would
        mark it as a restore candidate on the strength of a network problem.
        """
        return self._result(
            row,
            HealthState.UNKNOWN,
            message=str(exc),
            details={
                "transport_error": type(exc).__name__,
                "indeterminate": True,
                "reason": "the check could not be executed, so nothing was learned",
            },
        )


_REGISTRY: dict[ProbeKind, type[Probe]] = {}


def register_probe(cls: type[Probe]) -> type[Probe]:
    """Class decorator: make a probe discoverable by its ``kind``."""
    existing = _REGISTRY.get(cls.kind)
    if existing is not None and existing is not cls:
        raise RuntimeError(
            f"two probe classes claim kind {cls.kind!r}: " f"{existing.__name__} and {cls.__name__}"
        )
    _REGISTRY[cls.kind] = cls
    return cls


def get_probe_class(kind: ProbeKind) -> type[Probe] | None:
    return _REGISTRY.get(kind)


def registered_kinds() -> frozenset[ProbeKind]:
    return frozenset(_REGISTRY)


__all__ = [
    "Probe",
    "TransportUnreachable",
    "get_probe_class",
    "register_probe",
    "registered_kinds",
]
