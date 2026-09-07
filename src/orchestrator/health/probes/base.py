"""Probe base class. Concrete probes land in phase 4.

Registering a new probe kind for phase 4+ is a two-step add:

1. Extend :class:`orchestrator.domain.enums.ProbeKind` with the new value.
2. Add a Pydantic config in :mod:`orchestrator.domain.schemas` and a subclass
   of :class:`Probe` here that consumes it.

The engine dispatches by ``ProbeKind`` — no orchestrator-wide changes needed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from orchestrator.db.models import Probe as ProbeRow
from orchestrator.domain.schemas import ProbeResult


class Probe(ABC):
    kind: str

    @abstractmethod
    async def run(self, row: ProbeRow) -> ProbeResult: ...
