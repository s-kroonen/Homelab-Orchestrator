"""Wake pipeline — seam for phase 3.

Contract (per spec §4, the ``/wake`` endpoint):
    resolve service -> node + guest
    -> return an immediate self-refreshing status page (handled by web layer)
    -> publish power-on to the power manager
    -> assert a hold
    -> poll the health engine until HEALTHY or timeout
    -> release the hold if timed out; the maintenance page transitions to failed/timeout
"""

from __future__ import annotations


class WakePipeline:
    async def run(self, service_slug: str) -> None:
        raise NotImplementedError("WakePipeline lands in phase 3.")
