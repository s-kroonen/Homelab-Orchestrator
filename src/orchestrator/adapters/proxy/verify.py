"""Did Traefik actually accept the router we just wrote?

Traefik Manager's ``/save`` happily writes a router that Traefik then refuses
(the incident that motivated this: ``entryPoint "https" doesn't exist · no
valid entryPoint for this router``). The refusal is only visible on the
*runtime* router list, so after a write we poll that list - Traefik needs a
moment to reload the file provider - and report what it says.

:func:`verify_route` is backend-neutral (it only uses
:meth:`ProxyAdapter.list_runtime_routers`) and is reused by create, edit and
Resync so every surface shows the same per-host status.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from orchestrator.adapters.proxy.base import ProxyAdapter, RouteInfo

#: Traefik reloads a file-provider change within a second or two.
VERIFY_TIMEOUT_S = 5.0
VERIFY_INTERVAL_S = 0.5

# RouteVerification.state values.
OK = "ok"
ERROR = "error"
#: Runtime routers were readable but ours is not among them (yet).
NOT_LOADED = "not_loaded"
#: The runtime list could not be read at all.
UNVERIFIED = "unverified"


@dataclass(frozen=True)
class RouteVerification:
    state: str
    #: Traefik's own error text when ``state == "error"``, else a note.
    message: str = ""
    router: str | None = None
    status: str | None = None
    entry_points: tuple[str, ...] = ()
    cert_resolver: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == OK

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "message": self.message,
            "router": self.router,
            "status": self.status,
            "entry_points": list(self.entry_points),
            "cert_resolver": self.cert_resolver,
        }


def bare_router_name(name: str) -> str:
    """``orchestrator-app@file`` -> ``orchestrator-app``."""
    return name.split("@", 1)[0]


def match_runtime_router(
    routers: Iterable[RouteInfo], *, route_ref: str | None, hostname: str | None
) -> RouteInfo | None:
    """The runtime router for a proxy host: by the stored route reference
    first (file provider), else the first router whose rule names the
    hostname."""
    routers = list(routers)
    if route_ref:
        for info in routers:
            if bare_router_name(info.name) == route_ref and info.name.endswith("@file"):
                return info
        for info in routers:
            if bare_router_name(info.name) == route_ref:
                return info
    if hostname:
        host = hostname.strip().lower()
        for info in routers:
            if host in info.hostnames:
                return info
    return None


def router_state(
    info: RouteInfo | None,
    *,
    expect_entry_points: Sequence[str] | None = None,
) -> RouteVerification:
    """Classify one runtime router (``None`` = Traefik does not have it)."""
    if info is None:
        return RouteVerification(NOT_LOADED, "Traefik has not loaded a router for this host (yet).")
    common = {
        "router": info.name,
        "status": info.status,
        "entry_points": info.entry_points,
        "cert_resolver": info.cert_resolver,
    }
    if info.error:
        return RouteVerification(ERROR, info.error, **common)  # type: ignore[arg-type]
    if info.status is not None and info.status != "enabled":
        return RouteVerification(
            ERROR,
            f"Traefik reports the router as {info.status}.",
            **common,  # type: ignore[arg-type]
        )
    if (
        expect_entry_points
        and info.entry_points
        and set(info.entry_points) != set(expect_entry_points)
    ):
        # Still the previous configuration: the reload has not happened yet.
        return RouteVerification(
            NOT_LOADED,
            "Traefik is still serving the previous entry points "
            f"({', '.join(info.entry_points)}).",
            **common,  # type: ignore[arg-type]
        )
    return RouteVerification(OK, "", **common)  # type: ignore[arg-type]


async def verify_route(
    proxy: ProxyAdapter,
    *,
    route_ref: str | None,
    hostname: str | None,
    expect_entry_points: Sequence[str] | None = None,
    timeout_s: float | None = None,
    interval_s: float | None = None,
    sleep: Callable[[float], object] = asyncio.sleep,
) -> RouteVerification:
    """Poll the runtime router until it is healthy or ``timeout_s`` passes.

    * ``ok`` - router found, enabled, no error (and bound to the entry points
      we wrote, when it reports them).
    * ``error`` - still erroring/disabled at the deadline; ``message`` is
      Traefik's own text.
    * ``not_loaded`` - runtime list readable but the router never appeared.
    * ``unverified`` - the runtime list could not be read at all.
    """
    timeout = VERIFY_TIMEOUT_S if timeout_s is None else timeout_s
    interval = VERIFY_INTERVAL_S if interval_s is None else interval_s
    waited = 0.0
    while True:
        routers = await proxy.list_runtime_routers()
        if routers is None:
            # Not worth polling: the view itself is unavailable.
            return RouteVerification(UNVERIFIED, "Could not read Traefik's runtime routers.")
        last = router_state(
            match_runtime_router(routers, route_ref=route_ref, hostname=hostname),
            expect_entry_points=expect_entry_points,
        )
        if last.ok:
            return last
        if waited >= timeout:
            return last
        result = sleep(interval)
        if asyncio.iscoroutine(result):
            await result
        waited += interval
