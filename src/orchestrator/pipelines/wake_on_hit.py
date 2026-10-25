"""Wake-on-hit: traffic reached a proxy host whose service looks down, so wake it.

The decision lives here so every source of "there was traffic" shares it: the Prometheus
:class:`~orchestrator.scheduler.traffic_collector.TrafficCollector` (a request counter that
went up) and the :class:`~orchestrator.scheduler.access_log_collector.AccessLogCollector` (new
access-log lines for the host). The caller decides *whether there was new traffic*; this module
decides whether that traffic may wake anything:

* ``ProxyHost.wake_on_hit`` (per route, default off) AND ``Service.wake_on_hit_enabled`` AND
  ``Node.wake_on_hit_enabled`` - the three-way gate of docs/traffic_metrics.md;
* only a service whose ``wake_trigger`` is ``call`` (a Start-button service is never woken by
  traffic alone);
* only when it looks down (:func:`is_down`).

Dedup and cooldown are not done here: ``WakePipeline.get_or_start`` refuses a second run while
one is running or within ``WAKE_RETRY_COOLDOWN_S`` of the last, so two collectors seeing the same
hit start one wake.
"""

from __future__ import annotations

from sqlmodel import Session

from orchestrator.db.models import Node, ProxyHost, Service, ServiceState
from orchestrator.domain.enums import HealthState, WakeTrigger
from orchestrator.logging_setup import get_logger
from orchestrator.pipelines.wake import WakeError, WakePipeline
from orchestrator.web.wake_support import fire_background

log = get_logger(__name__)

#: Actor recorded on PipelineRun / audit rows for this path, distinguishing it from actor="api"
#: (the manual/API wake) and actor="scheduler" (backups).
WAKE_ON_HIT_ACTOR = "traefik_hit"


def is_down(session: Session, service: Service) -> bool:
    """ "Down" for wake-on-hit purposes: a definite FAILED verdict, or no verdict has ever been
    recorded at all (never scanned, so no evidence it is up - safe to try, and ``get_or_start``'s
    cooldown stops this from being tried repeatedly).

    Deliberately NOT true for an existing UNKNOWN verdict: that can mean a blocked dependency, a
    probe/transport config error or a network blip unrelated to the node's power state, and an
    unwanted power-on is a much more expensive mistake than a skipped backup.

    DOWN (the guest gate: node offline or guest not running) counts too - it is the most direct
    evidence there is that a wake is what is needed.
    """
    state = session.get(ServiceState, service.id)
    if state is None:
        return True
    return state.last_verdict in (HealthState.FAILED, HealthState.DOWN)


def maybe_wake(
    session: Session, pipeline: WakePipeline, proxy_host: ProxyHost, *, source: str
) -> str | None:
    """Wake the service behind ``proxy_host`` if the gates allow. Returns the slug when a NEW
    wake was started, else None. ``source`` is only for the log (``prometheus`` / ``access_log``).
    """
    if not proxy_host.wake_on_hit or proxy_host.service_id is None:
        return None
    service = session.get(Service, proxy_host.service_id)
    if service is None or service.id is None or not service.wake_on_hit_enabled:
        return None
    if service.wake_trigger is not WakeTrigger.CALL:
        return None
    if service.node_id is None:
        return None
    node = session.get(Node, service.node_id)
    if node is None or not node.wake_on_hit_enabled:
        return None
    if not is_down(session, service):
        return None

    try:
        run, started_new = pipeline.get_or_start(session, service.slug, actor=WAKE_ON_HIT_ACTOR)
    except WakeError as exc:
        # Disabled service, no node, ... - the manual wake would hit the same error; this just
        # never surfaces it to anyone.
        log.warning(
            "wake_on_hit.skipped",
            source=source,
            service=service.slug,
            proxy_host=proxy_host.hostname,
            error=str(exc),
        )
        return None
    if not started_new:
        return None
    log.info(
        "wake_on_hit.triggered",
        source=source,
        service=service.slug,
        proxy_host=proxy_host.hostname,
        run_id=run.id,
    )
    fire_background(pipeline, run, service.slug, actor=WAKE_ON_HIT_ACTOR)
    return service.slug
