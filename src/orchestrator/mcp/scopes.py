"""What a token may do.

A token carries a set of scopes. A role is only a named preset of them, picked on the consent
screen (or when a personal token is made); the scopes are what is enforced. ``propose`` is the
one that lets a token ASK for a dangerous action - a person still has to approve every such
request, so it never executes by itself.
"""

from __future__ import annotations

#: scope -> what it allows (shown on the consent screen and in the docs).
SCOPES: dict[str, str] = {
    "read": "See services, health, backups, runs, activity, Portainer and the infrastructure",
    "backup": "Start backups and propose restores",
    "deploy": "Run guarded deploys",
    "wake": "Wake services and nodes",
    "control": "Start, stop and restart services, stacks and containers; switch a service on or off",
    "configure": "Change settings: adopt Portainer stacks, set database dumps and deploy settings, "
    "a stack's Git branch and polling, which service a proxy host belongs to",
    "propose": "Ask for dangerous actions (restore, guest and node power, unguarded deploy, "
    "stack delete) - a person approves each one",
}

#: Asked for by clients that want long-lived access; always granted, never a permission.
OFFLINE_ACCESS = "offline_access"

ROLES: dict[str, tuple[str, ...]] = {
    "viewer": ("read",),
    "operator": ("read", "backup", "wake"),
    "deployer": ("read", "backup", "wake", "deploy", "control"),
    "admin": ("read", "backup", "wake", "deploy", "control", "configure", "propose"),
}

ROLE_BLURBS: dict[str, str] = {
    "viewer": "Looks only. Cannot change anything.",
    "operator": "Looks, starts backups, wakes things up.",
    "deployer": "Also deploys and starts / stops / restarts. Dangerous actions are refused.",
    "admin": "Everything, including changing settings and asking for dangerous actions (each "
    "one needs a person).",
}


def parse_scope(value: str | list[str] | None) -> list[str]:
    """The known scopes in a space-separated string / list, in canonical order, no duplicates."""
    if not value:
        return []
    items = value.split() if isinstance(value, str) else list(value)
    return [s for s in SCOPES if s in items]


def role_of(scopes: list[str]) -> str:
    """The preset these scopes equal, else ``custom``."""
    wanted = set(scopes)
    for name, preset in ROLES.items():
        if set(preset) == wanted:
            return name
    return "custom"


def scopes_for_role(role: str) -> list[str]:
    return list(ROLES.get(role, ()))
