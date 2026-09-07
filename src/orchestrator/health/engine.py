"""Health / scan engine. **Seam only in phase 1** — filled in during phase 4.

The public shape is fixed now so pipelines can import it without waiting.
"""

from __future__ import annotations

from orchestrator.db.models import Service
from orchestrator.domain.enums import HealthState
from orchestrator.domain.schemas import ServiceVerdict


class HealthEngine:
    """Runs a service's probes and returns a three-state verdict.

    Aggregation rule (per spec §4):
      * all REQUIRED probes HEALTHY -> HEALTHY
      * any REQUIRED probe FAILED   -> FAILED
      * else                        -> UNKNOWN
    """

    async def scan(self, service: Service) -> ServiceVerdict:
        raise NotImplementedError("HealthEngine is implemented in phase 4.")

    def aggregate(self, results: list[tuple[bool, HealthState]]) -> HealthState:
        """Aggregate ``(required, state)`` pairs into the service verdict."""
        required = [state for req, state in results if req]
        if not required:
            return HealthState.UNKNOWN
        if any(s is HealthState.FAILED for s in required):
            return HealthState.FAILED
        if all(s is HealthState.HEALTHY for s in required):
            return HealthState.HEALTHY
        return HealthState.UNKNOWN
