"""A service's recovery policy: what to try when it FAILS, how often, and when to stop."""

from __future__ import annotations

from typing import Any

#: restart          - restart THIS service through its own controller
#: restart_parent   - restart the nearest parent that has a controller
#: restart_guest    - restart the root's whole Proxmox guest (a bigger hammer: opt in)
#: propose_restore  - ask a human to approve restoring the latest verified backup
STEPS = ("restart", "restart_parent", "restart_guest", "propose_restore")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "steps": ["restart"],
    "max_attempts": 0,  # 0 = as many as there are steps
    "window_s": 3600,
    "settle_s": 120,
}


def normalize_policy(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate a ``recovery:`` block and fill in the defaults. ``{}`` (nothing
    configured) stays ``{}``: recovery is off unless a service asks for it."""
    if not raw:
        return {}
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ValueError(
            f"unknown recovery key(s): {', '.join(sorted(unknown))} "
            f"(known: {', '.join(DEFAULTS)})"
        )
    out = {**DEFAULTS, **raw}
    if not isinstance(out["enabled"], bool):
        raise ValueError("recovery 'enabled' must be true or false")
    steps = out["steps"]
    if not isinstance(steps, list) or not steps or not all(isinstance(s, str) for s in steps):
        raise ValueError("recovery 'steps' must be a non-empty list")
    bad = [s for s in steps if s not in STEPS]
    if bad:
        raise ValueError(f"unknown recovery step(s): {', '.join(bad)} (one of {', '.join(STEPS)})")
    for key, low in (("max_attempts", 0), ("window_s", 60), ("settle_s", 0)):
        try:
            out[key] = int(out[key])
        except (TypeError, ValueError):
            raise ValueError(f"recovery {key!r} must be a whole number") from None
        if out[key] < low:
            raise ValueError(f"recovery {key!r} must be at least {low}")
    out["steps"] = list(steps)
    return out


def is_enabled(raw: dict[str, Any] | None) -> bool:
    return bool(raw) and bool(raw.get("enabled", True))


def effective_max(policy: dict[str, Any], applicable: int) -> int:
    """Attempts allowed in one window: the cap if set, never more than the ladder."""
    cap = int(policy.get("max_attempts") or 0)
    return min(cap, applicable) if cap else applicable
