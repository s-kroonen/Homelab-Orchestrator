"""Reverse-proxy adapter interface.

Mirrors ``orchestrator.adapters.power``'s shape: an ABC pitched at domain
verbs ("what does drift detection / the maintenance-fallback feature
actually need"), not a 1:1 passthrough of one backend's REST shapes. Today's
only real implementation (:class:`~orchestrator.adapters.proxy.
traefik_manager.TraefikManagerProxyAdapter`) happens to be built on Traefik
Manager's YAML/`errors`-middleware model, but nothing in this interface
assumes that — a future NPM or Pangolin adapter has a completely different
data model (no YAML, no "errors middleware" concept) and must still be able
to implement this ABC.

This formalizes what used to be a standalone pair of modules
(``orchestrator.registry.traefik_manager_client`` /
``traefik_manager_sync``), built when there was only one backend to support.
Now that the actual need is named — room for Traefik Manager today, bare
Traefik, NPM, Pangolin, or something else later, without rewriting every
caller — see ``docs/roadmap.md``'s "generalize the adapter pattern" entry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RouteInfo:
    """One live route this backend currently knows about.

    Provider-neutral: ``name`` is whatever this backend calls that route
    (a Traefik router name, say), ``hostnames`` is every hostname it
    matches, and ``upstream`` is the resolved backend URL(s) — ``None`` when
    the backend reports the route but couldn't resolve what it points at
    (an unsupported load-balancer provider, no servers configured, etc.).
    Mirrors what ``orchestrator.registry.traefik_status.RouterInfo`` already
    provided for the direct-Traefik-API read path, which keeps that exact
    shape for its own callers (see that module).
    """

    name: str
    hostnames: set[str]
    upstream: str | None
    #: Entry points the route is bound to, when the backend reports them.
    entry_points: tuple[str, ...] = ()
    #: Runtime health, only filled by :meth:`ProxyAdapter.list_runtime_routers`:
    #: Traefik's own ``status`` ("enabled" / "disabled" / "warning") and its
    #: ``error`` text (joined), exactly what Traefik Manager's UI shows.
    status: str | None = None
    error: str | None = None
    #: The ``tls.certResolver`` the router carries, when reported.
    cert_resolver: str | None = None
    #: False when the backend reports the route as switched off (Traefik
    #: Manager lists a disabled route in ``/api/routes`` with
    #: ``"enabled": false``; Traefik itself then has no runtime router for it).
    enabled: bool = True
    #: Middleware names attached to the route as the backend reports them
    #: (``None`` = the backend did not say, so nothing can be concluded from
    #: their absence).
    middlewares: tuple[str, ...] | None = None
    #: Names among ``middlewares`` whose definition points at a service that
    #: does not exist (Traefik then drops every router using them).
    dangling_middlewares: tuple[str, ...] = ()
    #: The backend's own handle for the route, as it must be sent back to
    #: toggle/delete/edit it. Traefik Manager with several config files
    #: (``MULTI_CONFIG``) gives every route ``id = "<configfile>::<name>"``
    #: while ``name`` stays the bare router name; with one file they are equal.
    #: ``None`` when the backend does not distinguish (runtime routers).
    route_id: str | None = None
    #: The config file the route lives in (Traefik Manager's ``configFile``;
    #: empty with a single config file). Needed to edit/delete precisely.
    config_file: str = ""

    @property
    def ref(self) -> str:
        """What to send the backend to address exactly this route."""
        return self.route_id or self.name


def find_route(
    routes: Sequence[RouteInfo] | None, ref: str | None, hostname: str | None = None
) -> RouteInfo | None:
    """Resolve a stored or bare route reference to the route the backend lists.

    ``ref`` may be the backend's full id (``file.yml::name``) or just the bare
    name. An exact id match wins; several routes sharing a bare name (the same
    router in two config files) are told apart by ``hostname`` - the one whose
    rule matches it. ``None`` when nothing matches.
    """
    if not ref or not routes:
        return None
    cands = [r for r in routes if ref in (r.route_id, r.name)]
    if not cands:
        return None
    pool = [r for r in cands if r.route_id == ref] or cands
    if hostname and len(pool) > 1:
        host = hostname.strip().lower()
        by_host = [r for r in pool if host in {h.lower() for h in r.hostnames}]
        pool = by_host or pool
    return pool[0]


@dataclass(frozen=True)
class EntryPointInfo:
    """One entry point the reverse proxy listens on (Traefik: ``web`` ->
    ``:80``). ``address`` is whatever the proxy reports (``":443"``)."""

    name: str
    address: str = ""


#: Human labels for the sources a certificate resolver can be seen in (the key
#: is what :meth:`ProxyAdapter.list_cert_resolver_sources` tags a name with).
CERT_RESOLVER_SOURCE_LABELS = {
    "tm_settings": "Traefik Manager settings",
    "static_config": "static config",
    "runtime_router": "used by a router",
    "default_setting": "TRAEFIK_DEFAULT_CERT_RESOLVER",
}


@dataclass(frozen=True)
class ConfigFiles:
    """The config files the proxy manager can write routes into (Traefik
    Manager: its ``CONFIG_PATHS`` / ``CONFIG_DIR``), as bare file names.

    ``can_create`` says whether a name that is not in ``files`` would be created
    (Traefik Manager with ``CONFIG_DIR`` set creates ``<name>.yml`` there) or
    silently replaced by its DEFAULT file (it has only a fixed ``CONFIG_PATHS``
    list - then an unknown name must be refused, never posted). ``None`` =
    could not tell. ``default`` is the file a save with no file name lands in.
    """

    files: tuple[str, ...] = ()
    can_create: bool | None = None
    default: str = ""

    @property
    def single(self) -> bool:
        """Exactly one fixed file: there is no choice to make."""
        return len(self.files) <= 1 and self.can_create is False


@dataclass(frozen=True)
class RouteOptions:
    """How a live route is built beyond hostname/upstream/entry points.

    The defaults are the safe ones for a typical reverse-proxied app:

    * ``pass_host_header`` True: the backend sees the hostname the visitor
      typed. With False the proxy sends the upstream's own address as ``Host``,
      so any backend that builds redirects or absolute links from it (Proxmox,
      PBS, most web UIs) bounces the browser to a raw LAN IP.
    * ``skip_upstream_tls_verify``: only meaningful for an ``https://``
      upstream with a self-signed certificate (PBS, Proxmox).
    * ``tls_main`` / ``tls_sans``: request one certificate covering these names
      (e.g. ``kroon-en.nl`` + ``*.kroon-en.nl``) instead of one per host.
      Needs a cert resolver able to do it (DNS challenge for a wildcard).
    * ``http_redirect``: also answer plain HTTP and redirect it to HTTPS.
    * ``config_file``: the proxy manager's config file this host's route should
      live in (Traefik Manager: ``media.yml``). Blank = the configured template
      (``TRAEFIK_MANAGER_CONFIG_FILE``). It decides where a NEW route is created;
      an existing route stays where it is until it is explicitly moved. It is a
      property of the record, not of the route's content.
    """

    pass_host_header: bool = True
    skip_upstream_tls_verify: bool = False
    tls_main: str = ""
    tls_sans: tuple[str, ...] = ()
    http_redirect: bool = False
    config_file: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RouteOptions:
        data = data or {}
        sans = data.get("tls_sans") or ()
        if isinstance(sans, str):
            sans = [s for s in sans.replace(",", "\n").splitlines()]
        return cls(
            pass_host_header=bool(data.get("pass_host_header", True)),
            skip_upstream_tls_verify=bool(data.get("skip_upstream_tls_verify", False)),
            tls_main=str(data.get("tls_main") or "").strip(),
            tls_sans=tuple(s.strip() for s in sans if str(s).strip()),
            http_redirect=bool(data.get("http_redirect", False)),
            config_file=str(data.get("config_file") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pass_host_header": self.pass_host_header,
            "skip_upstream_tls_verify": self.skip_upstream_tls_verify,
            "tls_main": self.tls_main,
            "tls_sans": list(self.tls_sans),
            "http_redirect": self.http_redirect,
            "config_file": self.config_file,
        }

    def content_dict(self) -> dict[str, Any]:
        """:meth:`to_dict` without the fields that are not part of the route's
        content (``config_file`` only says where it is created)."""
        out = self.to_dict()
        out.pop("config_file", None)
        return out


@dataclass(frozen=True)
class AccessLogTail:
    """Result of :meth:`ProxyAdapter.fetch_access_log`.

    ``lines`` is the raw tail (``None`` when nothing could be read at all);
    ``error`` is a human-readable reason when the backend reported a problem
    — for Traefik Manager, its own ``error`` text verbatim (e.g. "Access log
    not found. Set ACCESS_LOG_PATH env var or configure the path in
    Settings."), so the operator sees exactly what to fix.
    """

    lines: list[str] | None
    error: str | None = None


@dataclass(frozen=True)
class SyncEvent:
    """One create/update/injection/removal a maintenance-fallback call
    performed, for the caller to audit-log. Mirrors the shape of the old
    ``orchestrator.registry.traefik_manager_sync.MaintenanceSyncEvent`` —
    callers (the proxy-hosts router, the CLI backfill command) feed this
    straight into ``orchestrator.audit.log.record``.
    """

    action: str
    name: str
    details: dict[str, Any] = field(default_factory=dict)


class ProxyRouteNotFound(Exception):
    """Backend-neutral "this route reference no longer exists" signal.

    :meth:`ProxyAdapter.update_route`/:meth:`ProxyAdapter.delete_route`
    implementations raise this (instead of, or wrapping, whatever
    backend-specific "not found" error they'd otherwise raise) so a caller
    (``web/routers/proxy_hosts.py``) can catch exactly this one case —
    "the route Traefik Manager was supposed to have is already gone" —
    without importing a specific adapter implementation's exception
    hierarchy. Every OTHER failure from these two methods should still be a
    real, visible error (see their docstrings) — this is narrowly for the
    one case a caller may reasonably treat as "already done".
    """


class ProxyAdapter(ABC):
    """All methods are async so callers stay non-blocking.

    ``hostname``/``service_slug`` below are the ``ProxyHost.hostname`` and
    the linked ``Service.slug`` — not anything backend-specific.
    """

    # Set False on adapters that are still a stub for a later phase. The
    # factory checks this and substitutes the dry-run implementation rather
    # than handing back an object whose start() raises and crash-loops the
    # container — same convention as ``orchestrator.adapters.power.base.
    # PowerAdapter.IMPLEMENTED``.
    IMPLEMENTED: bool = True
    #: True when ``RouteOptions.http_redirect`` is realised as a separate
    #: companion route this backend lists (Traefik Manager: ``<name>-redirect``),
    #: so its absence is a detectable difference.
    HAS_REDIRECT_COMPANION: bool = False

    async def start(self) -> None:  # noqa: B027 - deliberate default no-op, see docstring
        """Open any persistent resource this backend needs. Idempotent.

        Default no-op: every implementation shipped today talks to its
        backend over short-lived, per-call HTTP requests, the same
        reasoning ``orchestrator.registry.traefik_manager_client`` gave for
        not being an adapter in the first place — there is nothing to hold
        open between calls. A future backend that DOES need a persistent
        connection overrides this.
        """

    async def stop(self) -> None:  # noqa: B027 - deliberate default no-op, see start()
        """Close whatever :meth:`start` opened. Idempotent. Default no-op."""

    def maintenance_fallback_available(self) -> bool:
        """Is the maintenance-page fallback configured for this backend at all?
        (Traefik Manager needs its URL and ORCHESTRATOR_PUBLIC_URL.) When it is
        not, nothing about it is ever "pending"."""
        return True

    def route_ref_for(self, hostname: str) -> str:
        """The reference :meth:`create_route` will return for ``hostname``,
        known BEFORE anything is written. The DB stamps it onto a new
        live-managed host up front, so the saved YAML already carries it and a
        later push does not dirty the registry. Default: the hostname."""
        return hostname

    def owns_route_ref(self, route_ref: str) -> bool:
        """Does ``route_ref`` look like a route THIS orchestrator created (as
        opposed to one adopted from the backend, which is never overwritten or
        deleted)? Default: yes."""
        return True

    @abstractmethod
    async def list_routes(self) -> list[RouteInfo] | None:
        """Every live route this backend currently knows about.

        Returns ``None`` — never raises — when this backend is not
        configured or could not be reached, mirroring
        ``orchestrator.registry.traefik_status``'s existing
        degrade-to-"unknown" convention for drift detection (see
        ``docs/proxy_hosts.md``): this is the same read path, just behind
        the adapter now. A caller that needs a hard failure instead of a
        silent degrade (e.g. the admin-triggered Resync action) turns a
        ``None`` result into its own error.
        """

    def maintenance_middleware_name(self, service_slug: str) -> str | None:
        """The name of the middleware that carries the maintenance-page fallback
        for ``service_slug`` on a route, or ``None`` when this backend cannot say
        (then the sync state never concludes the rule is missing)."""
        return None

    @abstractmethod
    async def ensure_maintenance_fallback(
        self, *, hostname: str, service_slug: str, raise_errors: bool = False
    ) -> list[SyncEvent]:
        """Ensure a hit on ``hostname`` with a down backend falls back to
        this orchestrator's ``/maintenance/<service_slug>`` page. Idempotent
        — safe to call repeatedly for the same, unchanged pair.

        A backend with no equivalent capability returns ``[]`` and logs a
        warning rather than raising — same fail-open philosophy as the
        rest of this feature (see ``docs/proxy_hosts.md``'s "Fail-open, by
        design" section): this runs as a side effect of an ordinary
        ``ProxyHost`` create/update, never as an explicit admin-triggered
        write, so it must never block that request.
        """

    @abstractmethod
    async def remove_maintenance_fallback(
        self, *, hostname: str, service_slug: str
    ) -> list[SyncEvent]:
        """The inverse of :meth:`ensure_maintenance_fallback` — called on a
        ``ProxyHost`` rename (old hostname), re-link (old service) or
        unlink/delete. Same idempotency and fail-open contract."""

    # -- General route CRUD -------------------------------------------------
    #
    # Unlike the maintenance-fallback pair above (a quiet side effect of an
    # unrelated edit, fail-open by design), these three ARE the explicit
    # action the admin just took — creating, editing or deleting a
    # ``ProxyHost`` whose ``router_provider`` is this backend's. A failure
    # here must be visible to the caller, not swallowed: it RAISES on
    # failure rather than degrading to ``None``/``[]``. See
    # ``docs/proxy_hosts.md``'s "Live-managed proxy hosts" section.

    @abstractmethod
    async def create_route(
        self,
        *,
        hostname: str,
        upstream: str,
        entry_points: Sequence[str],
        cert_resolver: str = "",
        options: RouteOptions | None = None,
        config_file: str = "",
    ) -> str:
        """Create a real route on this backend for ``hostname`` -> ``upstream``,
        bound to exactly ``entry_points`` (never empty - an implementation
        raises ``ValueError`` rather than let the backend pick a default
        that may not exist) with TLS from ``cert_resolver`` (empty = none).
        ``config_file`` names the backend's config file to create it in (blank =
        the backend's default); an implementation that cannot write to that file
        raises ``ValueError`` naming the files it can, rather than put the route
        somewhere else.

        Returns an opaque, backend-specific reference (a route id) the
        caller MUST persist (``ProxyHost.extra``) in order to target this
        exact route again in a later :meth:`update_route`/
        :meth:`delete_route` call — this adapter does not re-derive a route
        from a hostname on its own. Raises on failure (network error,
        backend rejected the request, etc.) rather than returning a
        sentinel; the caller's create must fail visibly too.
        """

    @abstractmethod
    async def update_route(
        self,
        route_ref: str,
        *,
        hostname: str,
        upstream: str,
        entry_points: Sequence[str],
        cert_resolver: str = "",
        options: RouteOptions | None = None,
        previous_options: RouteOptions | None = None,
    ) -> None:
        """Update the route ``route_ref`` identifies to point at the given
        ``hostname``/``upstream``/``entry_points``/``cert_resolver`` (all of
        them, every time - an edit that omitted entry points would reset
        them on backends that default). Raises on failure, including
        :class:`ProxyRouteNotFound` if the backend no longer has a route
        for that reference (the caller decides what to do about that; see
        :meth:`delete_route`'s note on the same case)."""

    @abstractmethod
    async def delete_route(
        self, route_ref: str, *, with_redirect: bool = True, hostname: str | None = None
    ) -> None:
        """Delete the route ``route_ref`` identifies (``hostname`` helps pick
        the right one when a backend holds the same name twice).

        Raises on failure — EXCEPT raises :class:`ProxyRouteNotFound`
        specifically (not a generic failure) when the backend no longer has
        a route for that reference, so a caller deleting its own local
        record can choose to treat "already gone on the backend" as
        success rather than blocking the local delete on it.
        """

    async def move_route(
        self,
        route_ref: str,
        *,
        hostname: str,
        upstream: str,
        entry_points: Sequence[str],
        cert_resolver: str = "",
        options: RouteOptions | None = None,
        config_file: str,
    ) -> str:
        """Re-save the route ``route_ref`` identifies into ``config_file`` (and
        take it out of the file it was in), together with its companion
        redirect route, then READ IT BACK. Returns the file it now lives in.
        An explicit, opt-in action: raises on any failure, including a route
        that is not where it was expected afterwards. Default: the backend has
        no config files to move between."""
        raise NotImplementedError(f"{type(self).__name__} cannot move a route between config files")

    async def list_config_files(self) -> ConfigFiles | None:
        """The config files routes can be written into, or ``None`` when this
        backend has no such notion or cannot be read (callers then skip the
        check and offer free text)."""
        return None

    async def set_route_enabled(
        self,
        route_ref: str,
        enabled: bool,
        *,
        with_redirect: bool = False,
        hostname: str | None = None,
    ) -> None:
        """Switch the route ``route_ref`` off (``enabled=False``) or back on,
        WITHOUT deleting it: the backend keeps everything needed to bring the
        exact same route back. ``with_redirect`` also toggles the companion
        plain-HTTP -> HTTPS router (``RouteOptions.http_redirect``).

        An explicit admin action like the CRUD trio above: raises on failure -
        and an implementation must read the state back rather than trust a
        success response (a toggle the backend silently ignored is a failure).
        Default: the backend cannot do this, so it says so rather than
        pretending (the caller turns it into a visible error)."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support enabling/disabling a route"
        )

    # -- Access-log read path (GeoIP traffic visibility) --------------------

    @abstractmethod
    async def tail_access_log(self, lines: int = 200) -> list[str] | None:
        """The most recent ``lines`` lines of Traefik's own access log, as
        raw strings — one JSON or combined-format log line per entry,
        whichever format Traefik itself is configured to write (see
        ``orchestrator.registry.access_log`` for the parser).

        This is a passive READ, same degrade convention as
        :meth:`list_routes`: returns ``None`` — never raises — when this
        backend is not configured, cannot be reached, or (for
        :class:`~orchestrator.adapters.proxy.traefik_manager
        .TraefikManagerProxyAdapter` specifically) has no access-log path
        configured on the gateway. The access-log collector (``orchestrator.scheduler
        .access_log_collector``) uses :meth:`fetch_access_log` instead, which
        also says WHY nothing came back; ``None`` here is the same signal,
        never an exception. There is no "since" parameter —
        every poll re-fetches the same tail, and it is the caller's job to
        dedupe against what it already processed (see that collector's
        cursor).
        """

    async def fetch_access_log(self, lines: int = 200) -> AccessLogTail:
        """Like :meth:`tail_access_log`, but says WHY nothing came back.

        The access-log collector uses this so a misconfiguration (no access
        log path on the gateway, path refused, backend unreachable) is
        visible in its status instead of looking exactly like "no traffic".
        Never raises. The default wraps :meth:`tail_access_log` for backends
        that cannot tell the two apart; Traefik Manager overrides it to
        surface its own ``error`` text.
        """
        raw = await self.tail_access_log(lines=lines)
        if raw is None:
            return AccessLogTail(
                lines=None, error="This proxy backend did not return an access log."
            )
        return AccessLogTail(lines=raw)

    async def list_runtime_routers(self) -> list[RouteInfo] | None:
        """Every LIVE router the reverse proxy itself is serving, across all
        of its providers (file, docker, ...), named exactly as the proxy's
        own access log names them (``name@provider`` for Traefik).

        This is the read the access-log collector uses to resolve a log
        line's router name to hostnames — :meth:`list_routes` only covers
        the routes this backend manages itself (Traefik Manager: file
        provider, bare names). ``upstream`` may be ``None``. Default:
        ``None`` (backend has no such view); never raises.
        """
        return None

    async def list_entrypoints(self) -> list[EntryPointInfo] | None:
        """The entry points the proxy really listens on, for the add/edit form
        and for validation. ``None`` - never raises - when this backend has no
        such view or cannot be reached; callers then fall back to free text."""
        return None

    async def list_cert_resolver_sources(self) -> dict[str, list[str]] | None:
        """Certificate resolver names this backend knows about, each with the
        sources that report it (stable order, de-duplicated), or ``None`` when
        no source can be read (the caller then offers a free-text field)."""
        return None

    async def list_cert_resolvers(self) -> list[str] | None:
        """Certificate resolver names this backend knows about, or ``None``
        when it cannot say (the caller then offers a free-text field)."""
        sources = await self.list_cert_resolver_sources()
        return None if sources is None else list(sources)

    async def list_domains(self) -> list[str] | None:
        """Base domains the operator serves (Traefik Manager's configured
        domains, e.g. ``kroon-en.nl``) - the form offers ``domain`` +
        ``*.domain`` as a ready-made wildcard certificate. ``None`` = unknown."""
        return None

    async def list_middlewares(self) -> list[str] | None:
        """Names of the HTTP middlewares the proxy currently has (with their
        ``@provider`` suffix, e.g. ``redirect-to-https@file``), or ``None``
        when it cannot say. Used to refuse "redirect HTTP to HTTPS" when the
        redirect middleware does not exist."""
        return None
