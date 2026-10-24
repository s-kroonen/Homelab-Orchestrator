"""A service's ``deploy:`` block: how it is deployed and what guards the deploy.

Everything here is the orchestrator's own - Portainer holds the stack, its Git URL and
its environment. ``{}`` (nothing configured) means the service has no deploy method.
"""

from __future__ import annotations

import copy
from typing import Any

PROVIDERS = ("generic", "gitlab", "github")
#: notify  - say so and leave it as it is
#: stop    - take the stack down and say so (fine for dev and test)
#: restore_backup - propose restoring the guest backup taken before, through the approval page
ON_FAILURE = ("notify", "stop", "restore_backup")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "trigger": {"webhook": None},
    "guard": {
        "pre": {"probes": False, "backup": None},
        "post": {"probes": True, "timeout_s": 180, "settle_s": 30},
    },
    "on_failure": "notify",
    "pull_image": True,
    "prune": False,
}

#: Named starting points for the wizard and the CLI.
PRESETS: dict[str, dict[str, Any]] = {
    "none": {"guard": {"pre": {"probes": False, "backup": None}, "post": {"probes": False}}},
    "probes": {"guard": {"pre": {"probes": True}, "post": {"probes": True}}},
    "probes+backup": {
        "guard": {
            "pre": {"probes": True, "backup": {"max_age_h": 24, "take_if_older": True}},
            "post": {"probes": True},
        }
    },
}


def _bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"deploy {what} must be true or false")
    return value


def _int(value: Any, what: str, low: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"deploy {what} must be a whole number")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"deploy {what} must be a whole number") from None
    if number < low:
        raise ValueError(f"deploy {what} must be at least {low}")
    return number


def _only(raw: dict[str, Any], known: tuple[str, ...], where: str) -> None:
    unknown = set(raw) - set(known)
    if unknown:
        raise ValueError(
            f"unknown {where} key(s): {', '.join(sorted(unknown))} (known: {', '.join(known)})"
        )


def _dict(value: Any, what: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"deploy {what} must be an object")
    return dict(value)


def normalize_deploy(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate a ``deploy:`` block and fill in the defaults. ``{}`` stays ``{}``."""
    if not raw:
        return {}
    cfg = _dict(raw, "block")
    _only(cfg, tuple(DEFAULTS), "deploy")
    out = copy.deepcopy(DEFAULTS)
    out["enabled"] = _bool(cfg.get("enabled", True), "'enabled'")

    trigger = _dict(cfg.get("trigger"), "trigger")
    _only(trigger, ("webhook",), "deploy trigger")
    hook = trigger.get("webhook")
    if hook not in (None, False):
        hook = _dict(hook, "trigger.webhook")
        _only(hook, ("provider", "branch"), "deploy webhook")
        provider = str(hook.get("provider") or "generic").strip().lower()
        if provider not in PROVIDERS:
            raise ValueError(f"deploy webhook provider must be one of {', '.join(PROVIDERS)}")
        branch = hook.get("branch") or ""
        if not isinstance(branch, str):
            raise ValueError("deploy webhook branch must be text")
        out["trigger"]["webhook"] = {"provider": provider, "branch": branch.strip()}
    else:
        out["trigger"]["webhook"] = None

    guard = _dict(cfg.get("guard"), "guard")
    _only(guard, ("pre", "post"), "deploy guard")
    pre = _dict(guard.get("pre"), "guard.pre")
    _only(pre, ("probes", "backup"), "deploy guard.pre")
    out["guard"]["pre"]["probes"] = _bool(pre.get("probes", False), "guard.pre.probes")
    backup = pre.get("backup")
    if backup not in (None, False):
        backup = _dict(backup, "guard.pre.backup")
        _only(backup, ("max_age_h", "take_if_older"), "deploy guard.pre.backup")
        out["guard"]["pre"]["backup"] = {
            "max_age_h": _int(backup.get("max_age_h", 24), "guard.pre.backup.max_age_h", 1),
            "take_if_older": _bool(
                backup.get("take_if_older", True), "guard.pre.backup.take_if_older"
            ),
        }
    post = _dict(guard.get("post"), "guard.post")
    _only(post, ("probes", "timeout_s", "settle_s"), "deploy guard.post")
    out["guard"]["post"] = {
        "probes": _bool(post.get("probes", True), "guard.post.probes"),
        "timeout_s": _int(post.get("timeout_s", 180), "guard.post.timeout_s", 10),
        "settle_s": _int(post.get("settle_s", 30), "guard.post.settle_s", 0),
    }

    on_failure = str(cfg.get("on_failure", "notify")).strip().lower()
    if on_failure not in ON_FAILURE:
        raise ValueError(f"deploy on_failure must be one of {', '.join(ON_FAILURE)}")
    out["on_failure"] = on_failure
    out["pull_image"] = _bool(cfg.get("pull_image", True), "pull_image")
    out["prune"] = _bool(cfg.get("prune", False), "prune")
    return out


def check_target(deploy: dict[str, Any], controller: dict[str, Any]) -> None:
    """A deploy redeploys a stack, so the service must be linked to one."""
    if not deploy:
        return
    if controller.get("type") != "portainer" or not controller.get("stack"):
        raise ValueError(
            "a deploy method needs the service to be linked to a Portainer stack "
            "(controller: {type: portainer, endpoint: N, stack: NAME})"
        )


def preset(name: str) -> dict[str, Any]:
    """A normalised block for a named preset (``none``, ``probes``, ``probes+backup``)."""
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r} (one of {', '.join(PRESETS)})")
    return normalize_deploy(copy.deepcopy(PRESETS[name]))


def is_enabled(raw: dict[str, Any] | None) -> bool:
    return bool(raw) and bool(raw.get("enabled", True))


def webhook_of(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    hook = (raw or {}).get("trigger", {}).get("webhook")
    return dict(hook) if hook else None
