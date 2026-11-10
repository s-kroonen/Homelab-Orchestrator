"""Where a notification takes you.

Every alert is about one thing (a node, a guest, a service, a route) and what happened to
it: :func:`link_for` turns ``(level, target, event)`` into the dashboard page, and the spot
on it, where you can deal with that: the nearest page under the source it belongs to (a
node or guest on its Proxmox, a route on its proxy, a deploy on its stack's Portainer). The
feed, the activity page and the channels all use it, so tapping a push or an ntfy alert
opens the problem and not just the dashboard.

Paths are relative; :func:`absolute` adds the address the dashboard is reached on, for
the channels that live outside the browser (ntfy, e-mail, webhook).
"""

from __future__ import annotations

from urllib.parse import quote

from orchestrator.config import Settings
from orchestrator.connectors.ownership import node_href_by_name
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

#: Event -> the part of a service's page that shows it (an element id on that page; the
#: page opens the tab that holds it). Anything not listed lands on the health strip.
_SERVICE_SPOT = {
    "backup_failed": "backups",
    "backup_retry": "backups",
    "backup_skipped": "backups",
    "restore_succeeded": "backups",
    "restore_failed": "backups",
    "restore_proposed": "backups",
    "deploy_succeeded": "deploys",
    "deploy_failed": "deploys",
    "deploy_blocked": "deploys",
    "deploy_outside": "deploys",
    "dump_failed": "dumps",
    "dump_succeeded": "dumps",
    "switch_failed": "stack",
    "outside_change": "stack",
    "wake_failed": "health",
}

#: Events about a stack's deploys or its state in Portainer: for a service run as a stack they
#: land on the stack's page under its Portainer (the SourceStack wireframe), not the service's.
_STACK_EVENTS = {
    "deploy_failed",
    "deploy_blocked",
    "deploy_outside",
    "deploy_succeeded",
    "outside_change",
}


def _stack_page(slug: str) -> str | None:
    """The page of the stack a service runs as, or None (no stack, or no database to ask: a
    link is never worth failing an alert for)."""
    from sqlmodel import Session, select

    from orchestrator.connectors.registry import default_source
    from orchestrator.db.models import Service
    from orchestrator.db.session import get_engine

    try:
        with Session(get_engine()) as session:
            svc = session.exec(select(Service).where(Service.slug == slug)).first()
    except Exception as exc:
        log.debug("links.no_database", error=str(exc))
        return None
    cfg = (svc.controller or {}) if svc is not None else {}
    if cfg.get("type") != "portainer" or not cfg.get("stack"):
        return None
    try:
        endpoint = int(cfg["endpoint"])
    except (KeyError, TypeError, ValueError):
        return None
    # a link naming no Portainer is on the default one
    main = default_source("portainer")
    instance = str(cfg.get("instance") or "") or (main.id if main is not None else "")
    if not instance:
        return None
    return (
        f"/sources/{quote(instance, safe='')}/stacks/{endpoint}/{quote(str(cfg['stack']), safe='')}"
    )


def link_for(level: str, target: str, event: str = "") -> str | None:
    """The page for one alert, or None when nothing on the dashboard fits."""
    if event == "test":
        return "/notifications"
    if level in ("service", "sub"):
        if not target:
            return None
        if target == "orchestrator":  # the self-update announces under this name
            return "/system"
        if target == "mcp":  # an assistant's request that names no service
            return "/assistants"
        if event in _STACK_EVENTS:
            page = _stack_page(target)
            if page:
                return page
        spot = _SERVICE_SPOT.get(event, "health")
        return f"/services/{quote(target, safe='')}#{spot}"
    if level == "node":
        return node_href_by_name(target) if target else None
    if level in ("guest", "disk"):
        # its Proxmox source's guest list, filtered to this one
        node, _, vmid = target.partition("/")
        if not node:
            return None
        return node_href_by_name(node, tab="guests", find=vmid or node)
    if level == "route":
        # the proxy host, on the proxy that owns it
        return f"/find/proxy-host?hostname={quote(target, safe='')}" if target else None
    if level == "network":
        return "/system#h-net"
    return None


def origin(settings: Settings) -> str:
    """The address people reach the dashboard on (no trailing slash): ``EXTERNAL_URL``, else
    the passkey origin (the address people sign in on), and only then
    ``ORCHESTRATOR_PUBLIC_URL``, the address Traefik forwards to, often a LAN one a phone away
    from home cannot open."""
    passkey = settings.webauthn_origin.strip()
    if "://localhost" in passkey or "://127." in passkey:
        passkey = ""  # the default, never set
    return (
        (settings.external_url or passkey or settings.orchestrator_public_url).strip().rstrip("/")
    )


def absolute(settings: Settings, path: str | None) -> str | None:
    base = origin(settings)
    return f"{base}{path}" if base and path else None


__all__ = ["absolute", "link_for", "origin"]
