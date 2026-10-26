"""Validation for a proxy host, shared by the web API, the CLI and the loader.

Everything here is a pure function of its inputs - no I/O - so the same rules
apply wherever a proxy host is written, and every failure comes back as a
``{field: message}`` mapping the UI can render next to the offending field.

The rule that motivated this module: nothing is written to Traefik Manager
until it has been checked. A route whose entry point does not exist is
accepted by Traefik Manager's ``/save`` and then shows up as broken
(``entryPoint "https" doesn't exist``) - so the entry points are checked here,
against the live list, before ``/save`` is ever called.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any
from urllib.parse import urlsplit

from orchestrator.adapters.proxy.base import EntryPointInfo
from orchestrator.registry.config_files import normalize_config_file

#: Traefik's own API/dashboard entry point. Never a place to serve a router.
INTERNAL_ENTRYPOINT = "traefik"

# One DNS label: 1-63 chars, alnum at both ends, hyphens inside.
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
# Optional leading "*." for wildcard routes; at least one label. A single-label
# name ("nas") is allowed - internal split-horizon names are common here.
_HOSTNAME_RE = re.compile(rf"^(\*\.)?{_LABEL}(\.{_LABEL})*$")
_ENTRYPOINT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_RESOLVER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")


class ProxyValidationError(ValueError):
    """One or more fields are invalid. ``errors`` maps field -> message."""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))


# ---------------------------------------------------------------------------
# hostname
# ---------------------------------------------------------------------------


def normalize_hostname(value: str) -> str:
    """Lower-case, trim, drop one trailing dot; raise ``ValueError`` (with a
    message that says what is wrong) when it is not a plain DNS name."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Hostname is required.")
    if "://" in raw:
        raise ValueError(
            f"{value!r} must be a bare hostname - remove the scheme (http:// or https://)."
        )
    if any(ch.isspace() for ch in raw):
        raise ValueError(f"{value!r} must not contain spaces.")
    if "/" in raw or "?" in raw or "#" in raw:
        raise ValueError(f"{value!r} must be a bare hostname - remove the path.")
    if ":" in raw:
        raise ValueError(f"{value!r} must not include a port - routers match on the hostname only.")
    host = raw.lower().rstrip(".")
    if len(host) > 253 or not _HOSTNAME_RE.match(host):
        raise ValueError(
            f"{value!r} is not a valid hostname (expected e.g. app.example.com, "
            "optionally *.example.com)."
        )
    return host


# ---------------------------------------------------------------------------
# upstream
# ---------------------------------------------------------------------------


def validate_upstream(value: str) -> str:
    """``http(s)://host:port`` - raises ``ValueError`` with a friendly reason."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError(
            "Upstream is required for a live-managed route, e.g. http://10.0.0.5:8080."
        )
    if any(ch.isspace() for ch in raw):
        raise ValueError(f"Upstream {raw!r} must not contain spaces.")
    if "://" not in raw:
        raise ValueError(f"Upstream {raw!r} needs a scheme, e.g. http://{raw} (http or https).")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise ValueError(f"Upstream {raw!r} is not a valid URL (check the port).") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"Upstream scheme {parts.scheme!r} is not supported - use http or https.")
    if not parts.hostname:
        raise ValueError(f"Upstream {raw!r} has no host, e.g. http://10.0.0.5:8080.")
    if port is None:
        raise ValueError(
            f"Upstream {raw!r} is missing a port - add one, e.g. {parts.scheme}://{parts.hostname}:8080."
        )
    if port == 0:
        raise ValueError(f"Upstream {raw!r} has an invalid port.")
    return raw


# ---------------------------------------------------------------------------
# entry points / cert resolver
# ---------------------------------------------------------------------------


def parse_entry_points(value: str | Iterable[str] | None) -> list[str]:
    """Accept a comma string or a list; trim, drop blanks, de-duplicate
    (first position wins)."""
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    out: list[str] = []
    for item in items:
        name = str(item).strip()
        if name and name not in out:
            out.append(name)
    return out


def _names(known: Sequence[EntryPointInfo]) -> list[str]:
    return [ep.name for ep in known]


def validate_entry_points(
    entry_points: Sequence[str], known: Sequence[EntryPointInfo] | None
) -> str | None:
    """A message, or ``None`` when fine. ``known`` is the live list, or
    ``None`` when it could not be read (then only the shape is checked)."""
    if not entry_points:
        avail = f" Available: {', '.join(_names(known))}." if known else ""
        return "Select at least one entry point." + avail
    for name in entry_points:
        if not _ENTRYPOINT_NAME_RE.match(name):
            return f"Entry point {name!r} is not a valid name."
    internal = [n for n in entry_points if n == INTERNAL_ENTRYPOINT]
    if internal:
        return (
            f'Entry point "{INTERNAL_ENTRYPOINT}" is Traefik\'s internal API/dashboard entry '
            "point and cannot serve a router - pick a web entry point instead."
        )
    if known is not None:
        valid = set(_names(known))
        unknown = [n for n in entry_points if n not in valid]
        if unknown:
            shown = ", ".join(f'"{n}"' for n in unknown)
            noun = "Entry point" if len(unknown) == 1 else "Entry points"
            verb = "does" if len(unknown) == 1 else "do"
            return (
                f"{noun} {shown} {verb} not exist on this Traefik. "
                f"Available: {', '.join(_names(known)) or '(none)'}"
            )
    return None


def validate_cert_resolver(resolver: str, known: Sequence[str] | None) -> str | None:
    resolver = (resolver or "").strip()
    if not resolver:
        return None
    if not _RESOLVER_RE.match(resolver):
        return f"Certificate resolver {resolver!r} is not a valid name."
    if known is not None and resolver not in known:
        return (
            f'Certificate resolver "{resolver}" is not known to Traefik (not in Traefik Manager\'s '
            "settings, Traefik's static config or any router). "
            f"Known: {', '.join(known) or '(none)'}"
        )
    return None


def resolve_default_entry_points(
    configured: Sequence[str], live: Sequence[EntryPointInfo] | None
) -> list[str]:
    """Pre-selection for a new host: the configured default if set (and still
    valid against the live list), else ``websecure`` if it exists, else the
    first live entry point that is not ``traefik``. ``[]`` means "no safe
    default - the operator has to choose"."""
    wanted = parse_entry_points(list(configured))
    if live is None:
        # Nothing to check against: trust the configured value, guess nothing.
        return [n for n in wanted if n != INTERNAL_ENTRYPOINT]
    names = _names(live)
    valid = [n for n in wanted if n in names and n != INTERNAL_ENTRYPOINT]
    if valid:
        return valid
    if "websecure" in names:
        return ["websecure"]
    for name in names:
        if name != INTERNAL_ENTRYPOINT:
            return [name]
    return []


# ---------------------------------------------------------------------------
# whole-record validation
# ---------------------------------------------------------------------------


def validate_route_options(
    options: dict[str, Any] | None,
    *,
    upstream: str,
    entry_points: Sequence[str],
    cert_resolver: str,
    known_entry_points: Sequence[EntryPointInfo] | None = None,
) -> dict[str, str]:
    """Problems with a host's route options, keyed by option name."""
    from orchestrator.adapters.proxy.base import RouteOptions
    from orchestrator.adapters.proxy.traefik_manager import is_plain_http_entrypoint

    errors: dict[str, str] = {}
    unknown = set(options or {}) - set(RouteOptions(hsts=True).to_dict())
    if unknown:
        errors["route_options"] = f"unknown option(s): {', '.join(sorted(unknown))}"
    opts = RouteOptions.from_dict(options)
    addresses = {e.name: e.address for e in known_entry_points or []}
    plain = [n for n in entry_points if is_plain_http_entrypoint(n, addresses.get(n, ""))]
    secure = [n for n in entry_points if n not in plain]

    if opts.skip_upstream_tls_verify and not upstream.strip().lower().startswith("https://"):
        errors["skip_upstream_tls_verify"] = (
            "Only applies to an https:// upstream (a self-signed Proxmox / PBS, say)."
        )
    if opts.tls_sans and not opts.tls_main:
        errors["tls_main"] = "Set the main domain too - the extra names (SANs) belong to it."
    if opts.tls_main:
        for field_name, value in (
            ("tls_main", opts.tls_main),
            *(("tls_sans", s) for s in opts.tls_sans),
        ):
            try:
                normalize_hostname(value)
            except ValueError as exc:
                errors[field_name] = f"{value!r}: {exc}"
        if not cert_resolver.strip():
            errors["tls_main"] = errors.get("tls_main") or (
                "A certificate for specific domains needs a certificate resolver to issue it - "
                "pick one above."
            )
        if not secure:
            errors["tls_main"] = errors.get("tls_main") or (
                "Certificate domains only apply to a TLS entry point (websecure)."
            )
    if opts.config_file:
        try:
            normalize_config_file(opts.config_file)
        except ValueError as exc:
            errors["config_file"] = str(exc)
    if opts.hsts and not secure:
        errors["hsts"] = "HSTS only means something over HTTPS: select an HTTPS entry point too."
    if opts.http_redirect and not secure:
        errors["http_redirect"] = (
            "There is nothing to redirect to: select an HTTPS entry point (websecure) as well."
        )
    return errors


def validate_proxy_host(
    *,
    hostname: str,
    upstream: str = "",
    entry_points: Sequence[str] = (),
    cert_resolver: str = "",
    live_managed: bool = True,
    known_entry_points: Sequence[EntryPointInfo] | None = None,
    known_cert_resolvers: Sequence[str] | None = None,
    taken_hostnames: Iterable[str] = (),
    route_options: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Every problem with a proxy host, keyed by field name; ``{}`` = valid.

    ``live_managed`` is True for a Traefik host that will be written to
    Traefik Manager (it has an upstream); only then are the upstream and entry
    point checks required. ``taken_hostnames`` are the OTHER proxy hosts'
    names (the caller excludes the record being edited).
    """
    errors: dict[str, str] = {}
    try:
        host = normalize_hostname(hostname)
    except ValueError as exc:
        errors["hostname"] = str(exc)
    else:
        if host in {t.strip().lower() for t in taken_hostnames}:
            errors["hostname"] = f"A proxy host for {host!r} already exists."
    if live_managed:
        try:
            validate_upstream(upstream)
        except ValueError as exc:
            errors["upstream"] = str(exc)
        msg = validate_entry_points(list(entry_points), known_entry_points)
        if msg:
            errors["entry_points"] = msg
        msg = validate_cert_resolver(cert_resolver, known_cert_resolvers)
        if msg:
            errors["cert_resolver"] = msg
        errors.update(
            validate_route_options(
                route_options,
                upstream=upstream,
                entry_points=entry_points,
                cert_resolver=cert_resolver,
                known_entry_points=known_entry_points,
            )
        )
    return errors


def raise_if_invalid(errors: dict[str, str]) -> None:
    if errors:
        raise ProxyValidationError(errors)
