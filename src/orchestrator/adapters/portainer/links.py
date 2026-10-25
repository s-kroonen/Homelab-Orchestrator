"""Where an object lives in Portainer's own UI: "open in Portainer" links."""

from __future__ import annotations

from orchestrator.adapters.portainer.base import Stack


def env_url(base: str, eid: int) -> str:
    return f"{base}/#!/{eid}/docker/dashboard" if base else ""


def stack_url(base: str, s: Stack) -> str:
    if not base:
        return ""
    if s.id is not None:
        return f"{base}/#!/{s.endpoint_id}/docker/stacks/{s.name}?id={s.id}&type=2&regular=true"
    return f"{base}/#!/{s.endpoint_id}/docker/stacks/{s.name}?type=2&external=true"


def container_url(base: str, eid: int, cid: str) -> str:
    return f"{base}/#!/{eid}/docker/containers/{cid}" if base else ""
