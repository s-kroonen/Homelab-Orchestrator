"""Where a notification takes you.

Every alert is about one thing (a node, a guest, a service) and what happened to it:
:func:`link_for` turns ``(level, target, event)`` into the dashboard page, and the spot
on it, where you can deal with that. The feed, the activity page and the channels all
use it, so tapping a push or an ntfy alert opens the problem and not just the dashboard.

Paths are relative; :func:`absolute` adds the address the dashboard is reached on, for
the channels that live outside the browser (ntfy, e-mail, webhook).
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from orchestrator.config import Settings

#: Event -> the part of a service's page that shows it (an element id on that page; the
#: page opens the tab that holds it). Anything not listed lands on the health strip.
_SERVICE_SPOT = {
    "backup_failed": "backups",
    "backup_retry": "backups",
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
        spot = _SERVICE_SPOT.get(event, "health")
        return f"/services/{quote(target, safe='')}#{spot}"
    if level == "node":
        return f"/nodes?{urlencode({'q': target})}" if target else None
    if level == "guest":
        # Nodes & guests, its guest list filtered to this one.
        node, _, vmid = target.partition("/")
        if not node:
            return None
        return f"/nodes?{urlencode({'q': vmid or node})}"
    return None


def origin(settings: Settings) -> str:
    """The address the dashboard is reached on (no trailing slash)."""
    return (
        (settings.external_url or settings.orchestrator_public_url or settings.webauthn_origin)
        .strip()
        .rstrip("/")
    )


def absolute(settings: Settings, path: str | None) -> str | None:
    base = origin(settings)
    return f"{base}{path}" if base and path else None


__all__ = ["absolute", "link_for", "origin"]
