"""Controllers: act on one service itself (see base.py)."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from orchestrator.config import Settings
from orchestrator.controllers.base import (
    ControllerError,
    ControllerStatus,
    ControllerUnavailable,
    ServiceController,
)
from orchestrator.db.models import Node, Service
from orchestrator.domain.enums import GuestKind
from orchestrator.registry.tree import effective_guest

CONTROLLER_TYPES = ("portainer", "haos", "docker", "systemd", "command")

_REQUIRED: dict[str, tuple[str, ...]] = {
    "portainer": ("endpoint",),
    "docker": ("container",),
    "systemd": ("unit",),
    "command": (),
    "haos": (),
}


def validate_controller(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise a ``controller:`` block, or raise ValueError saying what is wrong.
    ``{}`` (no controller) is valid."""
    if not raw:
        return {}
    cfg = dict(raw)
    kind = str(cfg.get("type", "")).strip().lower()
    if kind not in CONTROLLER_TYPES:
        raise ValueError(f"controller type must be one of {', '.join(CONTROLLER_TYPES)}")
    cfg["type"] = kind
    for field in _REQUIRED[kind]:
        if cfg.get(field) in (None, ""):
            raise ValueError(f"a {kind} controller needs {field!r}")
    if kind == "portainer":
        try:
            cfg["endpoint"] = int(cfg["endpoint"])
        except (TypeError, ValueError):
            raise ValueError("portainer 'endpoint' is the endpoint's number") from None
        if bool(cfg.get("container")) == bool(cfg.get("stack")):
            raise ValueError("a portainer controller needs exactly one of 'container' or 'stack'")
    if kind == "haos" and bool(cfg.get("addon")) == bool(cfg.get("core")):
        raise ValueError("a haos controller needs exactly one of 'addon' or 'core: true'")
    if kind == "command":
        if not any(cfg.get(v) for v in ("start", "stop", "restart", "status")):
            raise ValueError(
                "a command controller needs at least one of start / stop / restart / status"
            )
        for verb in ("start", "stop", "restart", "status"):
            if cfg.get(verb) is not None and not (
                isinstance(cfg[verb], list) and all(isinstance(a, str) for a in cfg[verb])
            ):
                raise ValueError(f"{verb} must be a list of arguments, not a shell string")
    if (
        kind in {"docker", "systemd", "command"}
        and cfg.get("transport") is not None
        and not isinstance(cfg["transport"], dict)
    ):
        raise ValueError("transport must be an object like a probe's")
    return cfg


def build_controller(
    settings: Settings,
    proxmox: Any,
    session: Session,
    service: Service,
    *,
    target: str = "service",
) -> ServiceController | None:
    """The controller for ``service``. ``target="guest"`` is its (root's) Proxmox
    guest. ``target="service"`` is its ``controller:`` block, falling back to the
    guest for a root service that has one and no controller of its own. None =
    there is nothing to act on."""
    from orchestrator.controllers.dry_run import DryRunController
    from orchestrator.controllers.guest import GuestController

    def guest() -> ServiceController | None:
        eg = effective_guest(session, service)
        node = session.get(Node, eg.node_id) if eg.node_id is not None else None
        if node is None or eg.kind is GuestKind.NONE or eg.guest_id is None:
            return None
        return GuestController(proxmox, node.name, eg.kind, eg.guest_id)

    if target == "guest":
        return guest()
    cfg = dict(service.controller or {})
    if not cfg:
        return guest() if service.parent_id is None else None
    cfg = validate_controller(cfg)
    kind = cfg["type"]
    if settings.dry_run and kind in {"portainer", "haos"}:
        return DryRunController(cfg)
    if kind == "portainer":
        from orchestrator.controllers.portainer import PortainerController

        return PortainerController(settings, cfg)
    if kind == "haos":
        from orchestrator.controllers.haos import HomeAssistantController

        return HomeAssistantController(settings, cfg)
    from orchestrator.controllers.command import CommandController
    from orchestrator.health.engine import TransportFactory, _spec_from_config

    transport = TransportFactory(settings).build(_spec_from_config(cfg))
    return CommandController(settings, cfg, transport)


__all__ = [
    "CONTROLLER_TYPES",
    "ControllerError",
    "ControllerStatus",
    "ControllerUnavailable",
    "ServiceController",
    "build_controller",
    "validate_controller",
]
