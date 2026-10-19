"""Auto-managed diagnostic HTTP probe for a linked :class:`ProxyHost`.

Traefik integration (drift detection, Resync, traffic metrics — see
``docs/proxy_hosts.md``) made a route's state visible on ``/proxy-hosts``,
but invisible to the health engine: a broken reverse-proxy route never
affected a service's own HEALTH verdict. This module closes that gap by
giving every ``ProxyHost`` linked to a ``Service`` a matching ``http`` probe
that checks the public route *through the gateway*, using exactly the
``url`` + ``host_header`` shape every other proxied HTTP probe in
``services.yaml`` already uses (see ``docs/probing_through_a_gateway.md``):

    url: "{PROBE_PROXY_BASE_URL}/"   # the gateway's LOCAL address
    host_header: "<the ProxyHost's hostname>"

No new probe kind, no new "severity" tier — this reuses ``ProbeKind.HTTP``
and ``Probe.required``. Every auto-created probe is ``required=False``: a
service can have several ``ProxyHost`` records (e.g. two hostnames fronting
the same app) and none of them individually has to be up for the service to
be healthy. An operator who wants one specific route's reachability to
actually gate a service flips ``required=True`` on that one probe via the
existing probe-edit UI — no code change needed. A later sync (Resync, a proxy
host edit) only rewrites the derived config, never ``required``.

Gateway-outage handling (UNKNOWN, not FAILED, downstream of a dead gateway)
falls out of ``Service.depends_on`` / the health engine for free, the same
way it already does for any other probe that routes through the gateway —
nothing here is gateway-aware on its own; it only has to point the probe's
``url`` at the gateway, which the ``probe_proxy_base_url`` + ``host_header``
shape above already does.

This whole module is a no-op when ``Settings.probe_proxy_base_url`` is
unset — same "empty disables it" convention as every other optional
Traefik/PBS/Proxmox integration in this app.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from orchestrator.config import Settings
from orchestrator.db.models import Probe
from orchestrator.domain.enums import ProbeKind
from orchestrator.logging_setup import get_logger
from orchestrator.registry.probe_specs import PROBE_SPECS

log = get_logger(__name__)

# Sits after any hand-written probes by convention; exact value doesn't
# matter since probe order is just display/run ordering, not a gate.
_AUTO_PROBE_ORDER = 1000


@dataclass(frozen=True)
class ProbeSyncEvent:
    """One create/update/delete a :func:`sync_traefik_probe` call performed.

    Callers (the proxy-host router) turn each of these into an audit entry
    using the same ``probe.created`` / ``probe.updated`` / ``probe.deleted``
    action names the manual probe-CRUD endpoints already use.
    """

    action: str  # "created" | "updated" | "deleted"
    probe_id: int
    service_id: int
    name: str
    config: dict[str, Any] | None = None


def probe_name_for_hostname(hostname: str) -> str:
    """The stable, recognizable name an auto-created probe gets.

    ``Probe.name`` is only unique per-service (see ``_check_probe_name_free``
    in ``web/routers/dashboard.py``), not globally, so this never collides
    with a hand-written probe unless someone deliberately names one
    ``traefik:<hostname>`` too.
    """
    return f"traefik:{hostname}"


#: This probe answers "is Traefik routing this host to a live backend at
#: all", not "does the app's homepage render". The bare root path almost
#: never returns a plain 200 for a real app — a login redirect, an auth
#: wall, a 404 at "/" with the real content elsewhere are all completely
#: normal and all prove the route works. Only a genuine gateway/server
#: error (5xx — Traefik's own "no backend"/"bad gateway" responses, or the
#: app itself erroring) means the route is actually broken, so every 2xx,
#: 3xx and 4xx counts as reachable.
_EXPECT_STATUS = list(range(200, 500))


def _probe_config(settings: Settings, hostname: str) -> dict[str, Any]:
    return {
        "url": settings.probe_proxy_base_url.rstrip("/") + "/",
        "host_header": hostname,
        "expect_status": _EXPECT_STATUS,
        # This is a diagnostic-only probe (required=False) hitting the
        # gateway's bare LOCAL ip, which often carries a self-signed cert or
        # one that only validates for the public hostname via SNI — and SNI
        # here follows host_header, not verify_tls. Erring toward "likely to
        # pass when the route is actually fine" is more useful than a false
        # FAILED from a cert mismatch that has nothing to do with the route
        # actually working. An operator who wants strict TLS verification can
        # edit this probe by hand, same as any other.
        "verify_tls": False,
    }


def _find(session: Session, service_id: int, name: str) -> Probe | None:
    return session.exec(
        select(Probe).where(Probe.service_id == service_id, Probe.name == name)
    ).first()


def _remove(session: Session, service_id: int, hostname: str) -> ProbeSyncEvent | None:
    name = probe_name_for_hostname(hostname)
    probe = _find(session, service_id, name)
    if probe is None or probe.id is None:
        return None
    event = ProbeSyncEvent("deleted", probe.id, service_id, name)
    session.delete(probe)
    return event


def _upsert(
    session: Session, service_id: int, hostname: str, settings: Settings
) -> ProbeSyncEvent | None:
    name = probe_name_for_hostname(hostname)
    config = _probe_config(settings, hostname)
    probe = _find(session, service_id, name)
    if probe is None:
        probe = Probe(
            service_id=service_id,
            name=name,
            kind=ProbeKind.HTTP,
            required=False,
            timeout_s=PROBE_SPECS[ProbeKind.HTTP].default_timeout_s,
            order=_AUTO_PROBE_ORDER,
            config=config,
        )
        session.add(probe)
        session.flush()
        assert probe.id is not None
        return ProbeSyncEvent("created", probe.id, service_id, name, config)

    # Only what this sync derives (the URL/Host header) is rewritten. ``required``
    # and ``timeout_s`` are the operator's: a Resync or proxy-host edit must never
    # undo "this route gates the service".
    changed = probe.config != config or probe.kind is not ProbeKind.HTTP
    if not changed:
        return None
    probe.config = config
    probe.kind = ProbeKind.HTTP
    session.add(probe)
    session.flush()
    return ProbeSyncEvent("updated", probe.id, service_id, name, config)


def sync_traefik_probe(
    session: Session,
    settings: Settings,
    *,
    hostname: str | None,
    service_id: int | None,
    old_hostname: str | None = None,
    old_service_id: int | None = None,
) -> list[ProbeSyncEvent]:
    """Create, update or remove the one auto-managed probe for a ``ProxyHost``.

    Call this with the ``ProxyHost``'s CURRENT ``(hostname, service_id)`` and,
    when it changed, its PREVIOUS values (``old_hostname``/``old_service_id``).
    Both pairs defaulting to the same values models "nothing about this
    ProxyHost's identity changed" — the right call for a brand new
    ``ProxyHost`` (nothing existed before) and for an idempotent re-sync /
    backfill pass (ensure the probe matches what it should be right now).

    Covers the full lifecycle:

    * **create** — ``old_*`` omitted/equal to the new values: upserts once.
    * **update, hostname unchanged** — upserts in place if the config drifted
      (e.g. ``PROBE_PROXY_BASE_URL`` changed).
    * **update, hostname changed, same service** — the old ``traefik:<old
      hostname>`` probe is removed and a fresh ``traefik:<new hostname>`` one
      created, rather than leaving a stale duplicate.
    * **update, re-linked to a different service** — the probe is removed
      from the old service and (re)created on the new one.
    * **update, service_id cleared** (``None``) — the old probe is removed;
      nothing is created (no service to attach it to).
    * **delete** — pass ``hostname=None, service_id=None`` with the
      ``ProxyHost``'s last known values as ``old_*``: only the removal runs.

    No-op, returns ``[]``, whenever ``settings.probe_proxy_base_url`` is
    unset (the whole feature is opt-in) or ``service_id``/``hostname`` and
    ``old_service_id``/``old_hostname`` are all unset (nothing to do, nothing
    to undo — the "exists unlinked at the DB level" case).
    """
    if not settings.probe_proxy_base_url:
        log.debug("traefik_probe.skipped reason=probe_proxy_base_url_unset")
        return []

    if old_service_id is None and old_hostname is None:
        old_service_id, old_hostname = service_id, hostname

    events: list[ProbeSyncEvent] = []

    moved_or_cleared = old_service_id is not None and old_service_id != service_id
    renamed_in_place = (
        not moved_or_cleared
        and old_hostname is not None
        and hostname is not None
        and old_hostname != hostname
    )

    if (moved_or_cleared or renamed_in_place) and old_service_id is not None and old_hostname:
        removed = _remove(session, old_service_id, old_hostname)
        if removed is not None:
            events.append(removed)

    if service_id is None or not hostname:
        return events

    upserted = _upsert(session, service_id, hostname, settings)
    if upserted is not None:
        events.append(upserted)
    return events
