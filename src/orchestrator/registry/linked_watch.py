"""Notice when a linked thing was changed outside the orchestrator.

Every health cycle, the observed state of each service with linked parts is compared with
what was asked for. When they disagree (the stack was stopped in Portainer, a route was
parked in Traefik Manager) and keep disagreeing on the next cycle, it is written to Activity
and announced - once. Nothing is ever changed back: the operator decides, with the *apply
on / apply off* buttons (which are just the switch). A change the orchestrator itself is in
the middle of making (a switch or deploy running) is not an outside change.
"""

from __future__ import annotations

from sqlmodel import Session, col, select

from orchestrator.adapters.factory import AdapterBundle
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.db.models import PipelineRun, Service
from orchestrator.domain.enums import PipelineKind, PipelineStatus
from orchestrator.health.guest_gate import GuestStateCache
from orchestrator.logging_setup import get_logger
from orchestrator.notify.hooks import announce
from orchestrator.registry.linked import gather_context, read_linked

log = get_logger(__name__)

#: Cycles a disagreement must be seen on before it is reported (one is a blip, and a
#: switch or a deploy passes through disagreeing states on purpose).
CONFIRM_CYCLES = 2


class LinkedWatcher:
    """In-memory on purpose: after a restart it simply learns the current state again
    (it only monitors; the rules for what a restart should bring back come later)."""

    def __init__(self) -> None:
        #: slug -> (drift text, cycles seen)
        self._seen: dict[str, tuple[str, int]] = {}
        #: slugs whose current drift has already been reported
        self._reported: set[str] = set()

    async def observe(
        self,
        session: Session,
        adapters: AdapterBundle,
        settings: Settings,
        guest_cache: GuestStateCache | None = None,
    ) -> int:
        """One pass. Returns how many new outside changes were reported. ``guest_cache`` is
        the cycle's single Proxmox read when called from the health scanner."""
        ctx = await gather_context(session, adapters, guest_cache, light=True)
        busy = {
            r.service_id
            for r in session.exec(
                select(PipelineRun).where(
                    col(PipelineRun.kind).in_([PipelineKind.SWITCH, PipelineKind.DEPLOY]),
                    PipelineRun.status == PipelineStatus.RUNNING,
                )
            ).all()
        }
        reported = 0
        live: set[str] = set()
        for svc in session.exec(select(Service).order_by(Service.slug)).all():
            subtree = {svc.id, *(d.id for d in ctx.tree.descendants(svc))}
            if subtree & busy:
                continue
            state = await read_linked(session, adapters, settings, svc, ctx)
            live.add(svc.slug)
            if not state.drift:
                if svc.slug in self._reported:
                    audit.record(
                        session,
                        actor="system",
                        action="service.outside_change_cleared",
                        target=f"service:{svc.slug}",
                        details={"state": state.state},
                    )
                    session.commit()
                self._seen.pop(svc.slug, None)
                self._reported.discard(svc.slug)
                continue
            text, cycles = self._seen.get(svc.slug, (state.drift, 0))
            cycles = cycles + 1 if text == state.drift else 1
            self._seen[svc.slug] = (state.drift, cycles)
            if cycles < CONFIRM_CYCLES or svc.slug in self._reported:
                continue
            self._reported.add(svc.slug)
            reported += 1
            audit.record(
                session,
                actor="system",
                action="service.outside_change",
                target=f"service:{svc.slug}",
                details={"state": state.state, "drift": state.drift, **state.as_dict()},
            )
            session.commit()
            await announce(
                session,
                level="sub" if svc.parent_id else "service",
                key=svc.slug,
                name=svc.name or svc.slug,
                event="outside_change",
                severity="warn",
                title=f"{svc.slug} was changed outside the orchestrator",
                body=state.drift,
                scope={"slug": svc.slug},
            )
        for gone in set(self._seen) - live:
            self._seen.pop(gone, None)
            self._reported.discard(gone)
        return reported
