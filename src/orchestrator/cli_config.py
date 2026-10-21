"""Interactive config builders — the `probe`, `proxy`, `service` and `policy` CLI groups.

Goal: an operator should never have to hand-edit ``services.yaml``. Everything
these commands write is validated before it touches disk, and every edit is
surgical, so existing comments and hand-tuning survive.

Every prompt-driven command also accepts flags, so the same operations work
non-interactively from a script or an Ansible task.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import click
from sqlmodel import Session, col, select

from orchestrator.adapters.factory import build_adapters
from orchestrator.audit import log as audit
from orchestrator.config import get_settings
from orchestrator.db.models import ProxyHost, Service
from orchestrator.db.session import build_engine, get_engine
from orchestrator.domain.enums import ProbeKind
from orchestrator.logging_setup import configure_logging
from orchestrator.registry.editor import RegistryEditError, RegistryEditor
from orchestrator.registry.ip_address import validate_ip_address
from orchestrator.registry.probe_specs import (
    PROBE_SPECS,
    PROXY_FIELDS,
    TRANSPORT_FIELDS,
    FieldSpec,
    kinds_by_menu_order,
)
from orchestrator.registry.proxy_validation import (
    normalize_hostname,
    parse_entry_points,
    resolve_default_entry_points,
    validate_proxy_host,
)

# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def registry_path(override: str | None = None) -> Path:
    return Path(override) if override else get_settings().services_yaml_path


def open_editor(path_override: str | None = None) -> RegistryEditor:
    try:
        return RegistryEditor(registry_path(path_override))
    except RegistryEditError as exc:
        raise click.ClickException(str(exc)) from exc


def save(editor: RegistryEditor, what: str) -> None:
    try:
        editor.save()
    except RegistryEditError as exc:
        raise click.ClickException(str(exc)) from exc
    click.secho(f"{what} -> {editor.path}", fg="green")


#: Set by the --non-interactive flag. An explicit switch rather than only
#: sniffing the terminal, because TTY detection is not reliable everywhere —
#: on Git Bash for Windows, `< /dev/null` still reports as a TTY.
_FORCE_NON_INTERACTIVE = False


def set_non_interactive(value: bool) -> None:
    global _FORCE_NON_INTERACTIVE
    _FORCE_NON_INTERACTIVE = value


def interactive_default() -> bool:
    """Prompt only when there is a human to answer.

    Scripted invocations fall back to presets and defaults instead of blocking
    forever on a prompt nobody can see.
    """
    if _FORCE_NON_INTERACTIVE:
        return False
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def ask(
    spec: FieldSpec,
    *,
    preset: Any = None,
    interactive: bool = True,
    suggestion: Any = None,
) -> Any:
    """Resolve one field: preset wins, then a prompt, then the default.

    ``suggestion`` replaces the spec's default — a value worked out from the
    operator's own setup, such as the proxy address for an HTTP probe.
    """
    if preset is not None:
        return spec.parse(preset) if spec.parse and isinstance(preset, str) else preset

    default = spec.default if suggestion is None else suggestion
    if not interactive:
        if spec.required and default in (None, "", []):
            raise click.UsageError(
                f"--set {spec.key}=VALUE is required when running non-interactively "
                f"({spec.prompt})"
            )
        return default

    if spec.help:
        click.secho(f"    {spec.help}", fg="cyan")

    shown = ",".join(str(x) for x in default) if isinstance(default, list) else default

    while True:
        raw = click.prompt(
            f"  {spec.prompt}",
            default="" if shown in (None, "") else str(shown),
            show_default=shown not in (None, ""),
            type=str,
        ).strip()

        if not raw:
            if spec.required:
                click.secho("    required — please give a value", fg="red")
                continue
            return default

        if spec.choices and raw not in spec.choices:
            click.secho(f"    must be one of: {', '.join(spec.choices)}", fg="red")
            continue

        if spec.parse:
            try:
                return spec.parse(raw)
            except (ValueError, TypeError) as exc:
                click.secho(f"    could not parse that: {exc}", fg="red")
                continue
        return raw


def collect(
    fields: tuple[FieldSpec, ...],
    presets: dict[str, Any] | None = None,
    *,
    interactive: bool = True,
    suggest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run through a field list, dropping keys the user left empty."""
    presets = presets or {}
    suggest = suggest or {}
    out: dict[str, Any] = {}
    for spec in fields:
        value = ask(
            spec,
            preset=presets.get(spec.key),
            interactive=interactive,
            suggestion=suggest.get(spec.key),
        )
        # Empty optional values are omitted rather than written as null, so the
        # generated YAML shows only what was actually configured.
        if value in (None, "", []) and not spec.required:
            continue
        out[spec.key] = value
    return out


def parse_selection(raw: str, count: int) -> set[int]:
    """Parse '1-5,8,12' or 'all' / 'none' into zero-based indices."""
    raw = raw.strip().lower()
    if raw in {"all", "*"}:
        return set(range(count))
    if raw in {"none", ""}:
        return set()

    chosen: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            try:
                lo, hi = int(start), int(end)
            except ValueError as exc:
                raise click.BadParameter(f"{part!r} is not a range like 3-7") from exc
            if lo > hi:
                lo, hi = hi, lo
            chosen.update(range(lo - 1, hi))
        else:
            try:
                chosen.add(int(part) - 1)
            except ValueError as exc:
                raise click.BadParameter(f"{part!r} is not a number") from exc

    out_of_range = {i + 1 for i in chosen if i < 0 or i >= count}
    if out_of_range:
        raise click.BadParameter(f"out of range: {sorted(out_of_range)} (have 1-{count})")
    return chosen


def choose_probe_kind(preset: str | None = None) -> ProbeKind:
    if preset:
        try:
            return ProbeKind(preset)
        except ValueError as exc:
            known = ", ".join(k.value for k in kinds_by_menu_order())
            raise click.BadParameter(f"unknown probe kind {preset!r}. Known: {known}") from exc

    if not interactive_default():
        raise click.UsageError(
            "--kind is required when running non-interactively. Known kinds: "
            + ", ".join(k.value for k in kinds_by_menu_order())
        )

    kinds = kinds_by_menu_order()
    click.echo("\nProbe kinds:")
    for i, kind in enumerate(kinds, start=1):
        spec = PROBE_SPECS[kind]
        click.echo(f"  {i:>2}. {kind.value:<18} {spec.summary}")
    click.echo("")
    index = click.prompt("  Which kind", type=click.IntRange(1, len(kinds)))
    return kinds[index - 1]


def build_probe(
    kind: ProbeKind,
    presets: dict[str, Any] | None = None,
    *,
    interactive: bool | None = None,
    suggest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble one probe entry, prompting only when there is a human present."""
    spec = PROBE_SPECS[kind]
    presets = presets or {}
    if interactive is None:
        interactive = interactive_default()

    if interactive:
        click.secho(f"\nConfiguring a {kind.value} probe — {spec.summary}", bold=True)
        if spec.caution:
            click.secho(f"  NOTE: {spec.caution}", fg="yellow")

    name = presets.get("name")
    if not name:
        name = (
            click.prompt("  Probe name", default=spec.suggested_name, type=str)
            if interactive
            else spec.suggested_name
        )

    if interactive and suggest:
        click.secho(
            "  Suggested values come from your setup (.env) and this service's proxy hosts.",
            fg="cyan",
        )
    config = collect(spec.fields, presets, interactive=interactive, suggest=suggest)

    if spec.needs_transport:
        if interactive:
            click.secho("\n  How should this command reach the guest?", bold=True)
        fields = TRANSPORT_FIELDS
        if spec.transport_choices:
            # Some kinds only make sense one way; do not offer a choice that
            # cannot work (ansible_ping has no meaning over raw ssh).
            fields = tuple(
                (
                    FieldSpec(
                        f.key,
                        f.prompt,
                        default=spec.transport_choices[0],
                        choices=spec.transport_choices,
                        help=f.help,
                    )
                    if f.key == "type"
                    else f
                )
                for f in TRANSPORT_FIELDS
            )
        transport = collect(fields, presets, interactive=interactive)
        transport = {**transport, **dict(spec.transport_defaults)}

        if transport.get("type") == "local":
            # A local transport needs no host/user/port; keep the YAML honest.
            transport = {"type": "local"}
        elif transport.get("type") == "ansible":
            # user/port belong to ssh; the inventory owns those for ansible, and
            # leaving them in would imply we honour them.
            transport = {
                k: v for k, v in transport.items() if k not in {"user", "port", "jump_host"}
            }
        elif transport.get("jump_host") == "-":
            # An explicit "no jump" — writes an empty string, which the transport
            # factory distinguishes from "unset, fall back to the default".
            transport["jump_host"] = ""
        config["transport"] = transport

    required = presets.get("required")
    if required is None:
        if interactive:
            click.secho(
                "\n  Required probes gate backups. A non-required probe is recorded\n"
                "  for the dashboard but never blocks one.",
                fg="cyan",
            )
            required = click.confirm("  Required (gates backups)?", default=True)
        else:
            # Default to gating. A probe nobody explicitly marked optional should
            # count toward the verdict — the safe direction.
            required = True

    timeout = presets.get("timeout_s")
    if not timeout:
        timeout = (
            click.prompt("  Timeout (seconds)", default=spec.default_timeout_s, type=int)
            if interactive
            else spec.default_timeout_s
        )

    return {
        "name": name,
        "kind": kind.value,
        "required": bool(required),
        "timeout_s": int(timeout),
        "order": int(presets.get("order") or 10),
        "config": config,
    }


def probe_suggestions(kind: ProbeKind, editor: RegistryEditor, slug: str) -> dict[str, Any]:
    """Defaults worth offering for this service, worked out from the setup.

    An HTTP probe through the reverse proxy needs the proxy's LOCAL address and the
    hostname it routes on. Both are already known — PROBE_PROXY_BASE_URL from
    `init`, the hostname from the service's proxy hosts — so offer them rather than
    making the operator retype them for every service.
    """
    out: dict[str, Any] = {}
    base = get_settings().probe_proxy_base_url
    if kind is ProbeKind.HTTP and base:
        out["url"] = base.rstrip("/") + "/"
        hosts = (editor.get_service(slug) or {}).get("proxy_hosts") or []
        if hosts and hosts[0].get("hostname"):
            out["host_header"] = str(hosts[0]["hostname"])
    return out


# ---------------------------------------------------------------------------
# probe group
# ---------------------------------------------------------------------------


@click.group()
def probe() -> None:
    """Add, list and remove health probes."""


@probe.command("add")
@click.argument("slug")
@click.option("--kind", default=None, help="Probe kind; prompts if omitted.")
@click.option("--name", default=None, help="Probe name.")
@click.option("--required/--not-required", default=None, help="Does it gate backups?")
@click.option("--timeout", "timeout_s", type=int, default=None)
@click.option(
    "--set",
    "overrides",
    multiple=True,
    metavar="KEY=VALUE",
    help="Preset a config field non-interactively; repeatable.",
)
@click.option("--file", "path_override", default=None, help="services.yaml to edit.")
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Never prompt: use --set/--kind values and defaults. For scripts.",
)
def probe_add(
    slug: str,
    kind: str | None,
    name: str | None,
    required: bool | None,
    timeout_s: int | None,
    overrides: tuple[str, ...],
    path_override: str | None,
    non_interactive: bool,
) -> None:
    """Add a probe to SLUG."""
    set_non_interactive(non_interactive)
    editor = open_editor(path_override)
    editor.require_service(slug)

    presets: dict[str, Any] = {}
    for item in overrides:
        key, _, value = item.partition("=")
        if not _:
            raise click.BadParameter(f"--set expects KEY=VALUE, got {item!r}")
        presets[key.strip()] = value
    if name:
        presets["name"] = name
    if required is not None:
        presets["required"] = required
    if timeout_s is not None:
        presets["timeout_s"] = timeout_s

    chosen_kind = choose_probe_kind(kind)
    probe_entry = build_probe(
        chosen_kind, presets, suggest=probe_suggestions(chosen_kind, editor, slug)
    )
    action = editor.upsert_probe(slug, probe_entry)

    click.echo("")
    click.secho(json.dumps(probe_entry, indent=2), fg="cyan")
    save(editor, f"probe {probe_entry['name']!r} {action} on {slug!r}")
    click.echo(f"\nTest it:  orchestrator-cli scan {slug}")


@probe.command("list")
@click.argument("slug", required=False)
@click.option("--file", "path_override", default=None)
def probe_list(slug: str | None, path_override: str | None) -> None:
    """List probes for SLUG, or for every service."""
    editor = open_editor(path_override)
    slugs = [slug] if slug else editor.service_slugs()

    for s in slugs:
        svc = editor.get_service(s)
        if svc is None:
            click.secho(f"{s}: not in the registry", fg="red")
            continue
        probes = svc.get("probes") or []
        gating = sum(1 for p in probes if p.get("required", True))
        if not probes:
            # This is the state that makes the gate refuse a backup, so say so.
            click.secho(f"{s}: no probes — backups will be REFUSED (UNKNOWN)", fg="yellow")
            continue
        click.secho(f"{s}: {len(probes)} probe(s), {gating} gating", bold=True)
        for p in probes:
            mark = "REQ " if p.get("required", True) else "diag"
            click.echo(f"    {mark} {p.get('name'):<22} {p.get('kind')}")


@probe.command("remove")
@click.argument("slug")
@click.argument("name")
@click.option("--file", "path_override", default=None)
def probe_remove(slug: str, name: str, path_override: str | None) -> None:
    """Remove probe NAME from SLUG."""
    editor = open_editor(path_override)
    if not editor.remove_probe(slug, name):
        raise click.ClickException(f"no probe named {name!r} on {slug!r}")
    save(editor, f"probe {name!r} removed from {slug!r}")


# ---------------------------------------------------------------------------
# proxy group
# ---------------------------------------------------------------------------


@click.group()
def proxy() -> None:
    """Add, list and remove proxy hosts (Traefik / Pangolin routes)."""


def _live_catalog() -> tuple[list[Any] | None, list[str] | None]:
    """The proxy's entry points and cert resolvers, via the configured proxy
    adapter (dry-run under DRY_RUN). ``(None, None)`` when unreadable - the
    caller then validates the shape only."""
    settings = get_settings()

    async def _go() -> tuple[list[Any] | None, list[str] | None]:
        adapters = build_adapters(settings)
        await adapters.proxy.start()
        try:
            return (
                await adapters.proxy.list_entrypoints(),
                await adapters.proxy.list_cert_resolvers(),
            )
        finally:
            await adapters.proxy.stop()

    try:
        return asyncio.run(_go())
    except Exception:  # never let a catalog read break a YAML edit
        return None, None


@proxy.command("entrypoints")
def proxy_entrypoints() -> None:
    """List the entry points (and cert resolvers) the proxy reports.

    Exits 1 with a message when the proxy cannot be read (Traefik Manager not
    configured, or Traefik behind it unreachable) so scripts can tell.
    """
    configure_logging()
    settings = get_settings()
    eps, resolvers = _live_catalog()
    if eps is None:
        click.secho(
            "Entry points unavailable: Traefik Manager is not configured "
            "(TRAEFIK_MANAGER_URL) or could not be reached.",
            fg="yellow",
            err=True,
        )
        raise SystemExit(1)
    click.secho("Entry points:", bold=True)
    for ep in eps:
        note = "  (Traefik's internal API/dashboard - not usable for routers)"
        click.echo(f"    {ep.name:<16} {ep.address}{note if ep.name == 'traefik' else ''}")
    click.secho("Certificate resolvers:", bold=True)
    if resolvers is None:
        click.echo("    (unknown - the manager does not report them)")
    elif not resolvers:
        click.echo("    (none configured)")
    else:
        for name in resolvers:
            click.echo(f"    {name}")
    default = resolve_default_entry_points(settings.default_entry_points_list, eps)
    click.echo(
        f"Default for a new host: {', '.join(default) if default else '(none - choose one)'}"
    )


@proxy.command("add")
@click.argument("slug")
@click.option("--hostname", default=None)
@click.option("--upstream", default=None)
@click.option("--provider", "router_provider", default=None)
@click.option(
    "--entry-point",
    "entry_points",
    multiple=True,
    help="Traefik entry point to bind to (repeatable, e.g. --entry-point websecure). "
    "See `proxy entrypoints`.",
)
@click.option(
    "--cert-resolver",
    default=None,
    help="Certificate resolver for the route's TLS (blank/omitted = none).",
)
@click.option("--file", "path_override", default=None)
@click.option("--non-interactive", is_flag=True, help="Never prompt. For scripts.")
def proxy_add(
    slug: str,
    hostname: str | None,
    upstream: str | None,
    router_provider: str | None,
    entry_points: tuple[str, ...],
    cert_resolver: str | None,
    path_override: str | None,
    non_interactive: bool,
) -> None:
    """Attach a proxy host to SLUG (or update it, if the hostname is already there).

    Validated before anything is written: hostname, upstream (http(s)://host:port),
    entry points (must exist on the live Traefik when it can be read, and never the
    internal `traefik` one) and cert resolver. This edits services.yaml only; the
    route itself is created through the dashboard / API.
    """
    set_non_interactive(non_interactive)
    editor = open_editor(path_override)
    editor.require_service(slug)

    interactive = interactive_default()
    if interactive:
        click.secho(f"\nProxy host for {slug!r}", bold=True)
        click.secho(
            "  The orchestrator does NOT configure your proxy — Pangolin owns routing.\n"
            "  This records the mapping so the dashboard can show it and phase 5 can\n"
            "  wire the maintenance page to the right hostname.",
            fg="cyan",
        )
    entry = collect(
        PROXY_FIELDS,
        {"hostname": hostname, "upstream": upstream, "router_provider": router_provider},
        interactive=interactive,
    )

    settings = get_settings()
    live_managed = entry.get("router_provider", "traefik") == "traefik" and bool(
        entry.get("upstream")
    )
    chosen = parse_entry_points(list(entry_points))
    resolver = (cert_resolver or "").strip()
    known_eps: list[Any] | None = None
    known_resolvers: list[str] | None = None
    if live_managed:
        known_eps, known_resolvers = _live_catalog()
        if not chosen:
            default = resolve_default_entry_points(settings.default_entry_points_list, known_eps)
            if interactive:
                if known_eps:
                    click.secho("    Available: " + ", ".join(e.name for e in known_eps), fg="cyan")
                raw = click.prompt(
                    "  Entry points (comma list)",
                    default=",".join(default),
                    show_default=bool(default),
                    type=str,
                )
                chosen = parse_entry_points(raw)
                if cert_resolver is None:
                    resolver = click.prompt(
                        "  Certificate resolver (blank = none)",
                        default=settings.traefik_default_cert_resolver,
                        show_default=False,
                        type=str,
                    ).strip()
            else:
                chosen = default  # the documented default rule; empty -> validation error below
                if cert_resolver is None:
                    resolver = settings.traefik_default_cert_resolver.strip()

    wanted_host = str(entry.get("hostname", "")).strip().lower()
    taken = [
        str(h.get("hostname", ""))
        for other in editor.service_slugs()
        for h in ((editor.get_service(other) or {}).get("proxy_hosts") or [])
        if not (other == slug and str(h.get("hostname", "")).lower() == wanted_host)
    ]
    errors = validate_proxy_host(
        hostname=entry.get("hostname", ""),
        upstream=entry.get("upstream", ""),
        entry_points=chosen,
        cert_resolver=resolver,
        live_managed=live_managed,
        known_entry_points=known_eps,
        known_cert_resolvers=known_resolvers,
        taken_hostnames=taken,
    )
    if errors:
        raise click.ClickException(
            "invalid proxy host:\n" + "\n".join(f"  {k}: {v}" for k, v in errors.items())
        )
    entry["hostname"] = normalize_hostname(entry["hostname"])
    if live_managed:
        entry["entry_points"] = chosen
        if resolver:
            entry["cert_resolver"] = resolver
    action = editor.upsert_proxy_host(slug, entry)
    save(editor, f"proxy host {entry['hostname']!r} {action} on {slug!r}")


@proxy.command("list")
@click.argument("slug", required=False)
@click.option("--file", "path_override", default=None)
def proxy_list(slug: str | None, path_override: str | None) -> None:
    """List proxy hosts for SLUG, or for every service."""
    editor = open_editor(path_override)
    slugs = [slug] if slug else editor.service_slugs()
    any_found = False
    for s in slugs:
        svc = editor.get_service(s)
        if svc is None:
            continue
        hosts = svc.get("proxy_hosts") or []
        if not hosts:
            continue
        any_found = True
        click.secho(f"{s}:", bold=True)
        for h in hosts:
            provider = h.get("router_provider", "traefik")
            eps = ",".join(h.get("entry_points") or [])
            suffix = f" ep={eps}" if eps else ""
            if h.get("cert_resolver"):
                suffix += f" resolver={h.get('cert_resolver')}"
            if h.get("enabled") is False:
                suffix += " DISABLED"
            click.echo(
                f"    {h.get('hostname'):<34} -> {h.get('upstream'):<28} [{provider}]{suffix}"
            )
    if not any_found:
        click.echo("(no proxy hosts configured)")


@proxy.command("remove")
@click.argument("slug")
@click.argument("hostname")
@click.option("--file", "path_override", default=None)
def proxy_remove(slug: str, hostname: str, path_override: str | None) -> None:
    """Remove HOSTNAME from SLUG."""
    editor = open_editor(path_override)
    if not editor.remove_proxy_host(slug, hostname):
        raise click.ClickException(f"no proxy host {hostname!r} on {slug!r}")
    save(editor, f"proxy host {hostname!r} removed from {slug!r}")


@proxy.command("resync-maintenance")
def proxy_resync_maintenance() -> None:
    """Backfill the maintenance middleware for every linked proxy host.

    ``ProxyAdapter.ensure_maintenance_fallback`` (``orchestrator.adapters.proxy``)
    only runs on a ``ProxyHost`` create/update/delete/re-link — see the
    lifecycle hooks in ``web/routers/proxy_hosts.py``. A host created before
    ``ORCHESTRATOR_PUBLIC_URL``/``TRAEFIK_MANAGER_URL`` were set, or before
    this feature existed at all, never gets the middleware injected until
    it is edited again. This command closes that gap in one pass: every
    ``ProxyHost`` with a linked ``service_id`` gets a no-op "re-sync" call
    for its CURRENT ``(hostname, service_slug)`` — exactly the shape the
    create lifecycle hook already uses. See
    docs/proxy_hosts.md#maintenance-page-fallback.

    This operates on the live DB directly, unlike ``proxy add/list/remove``
    above (which edit ``services.yaml``) — there is nothing to ``save()``
    here, and no YAML is touched.

    No-op, with a message, when ``TRAEFIK_MANAGER_URL`` or
    ``ORCHESTRATOR_PUBLIC_URL`` is unset — same convention as the adapter
    itself. Also a logged no-op (via ``DryRunProxyAdapter``) under
    ``DRY_RUN=true`` or ``PROXY_ADAPTER=dry_run`` — same convention as every
    other adapter-backed CLI command now that this goes through
    ``build_adapters()`` instead of calling Traefik Manager directly.
    """
    configure_logging()
    build_engine()
    settings = get_settings()

    if not settings.traefik_manager_url.strip() or not settings.orchestrator_public_url.strip():
        click.secho(
            "TRAEFIK_MANAGER_URL and ORCHESTRATOR_PUBLIC_URL must both be set — "
            "nothing to backfill without them (same no-op convention as "
            "the adapter itself). See "
            "docs/proxy_hosts.md#maintenance-page-fallback-never-overwrite-an-existing-traefik-config.",
            fg="yellow",
        )
        return

    async def _go() -> int:
        processed = 0
        adapters = build_adapters(settings)
        await adapters.proxy.start()
        try:
            with Session(get_engine()) as session:
                hosts = session.exec(
                    select(ProxyHost)
                    .where(col(ProxyHost.service_id).is_not(None))
                    .order_by(ProxyHost.hostname)
                ).all()
                if not hosts:
                    click.echo("No proxy hosts linked to a service — nothing to backfill.")
                    return 0
                for ph in hosts:
                    svc = session.get(Service, ph.service_id) if ph.service_id else None
                    if svc is None:
                        # service_id points at a row that no longer exists —
                        # same orphaned-but-DB-only state docs/proxy_hosts.md
                        # calls out elsewhere; nothing to sync for it.
                        continue
                    events = await adapters.proxy.ensure_maintenance_fallback(
                        hostname=ph.hostname, service_slug=svc.slug
                    )
                    processed += 1
                    click.secho(f"{ph.hostname} ({svc.slug})", bold=True)
                    if not events:
                        click.echo("    (no change)")
                    for event in events:
                        click.echo(f"    {event.action:<22} {event.name}")
                        # Same audit mechanism and action vocabulary as the
                        # web lifecycle hooks (web/routers/proxy_hosts.py's
                        # _audit_maintenance_sync_events) — a CLI-triggered
                        # resync is audited exactly like one triggered by an
                        # edit in the dashboard, just with actor="cli", the
                        # same convention `backup`/`wake` already use for a
                        # CLI-triggered action.
                        audit.record(
                            session,
                            actor="cli",
                            action=event.action,
                            target=f"traefik_manager:{event.name}",
                            details=event.details,
                        )
                session.commit()
        finally:
            await adapters.proxy.stop()
        return processed

    processed = asyncio.run(_go())
    click.secho(f"\n{processed} linked proxy host(s) processed.", fg="green", bold=True)


def _print_push_status(status: Any) -> None:
    if not status.reachable:
        click.secho(f"! {status.message or 'Traefik Manager could not be read.'}", fg="yellow")
    for label, items, colour in (
        ("pending", status.pending, "yellow"),
        ("detached", status.detached, "magenta"),
    ):
        for item in items:
            click.secho(f"{label:<9}{item['hostname']}  [{item['state']}]", fg=colour, bold=True)
            for change in item["changes"]:
                click.echo(f"         - {change}")
    for item in status.pending_deletes:
        click.secho(f"delete   {item['hostname']}  (route {item['route_ref']})", fg="red")
    click.echo(
        f"{status.in_sync} in sync, {len(status.pending)} pending, "
        f"{len(status.detached)} detached, {len(status.pending_deletes)} to delete"
    )


@proxy.command("status")
def proxy_status() -> None:
    """Show what is not pushed to Traefik Manager yet (the live DB vs the gateway).

    Edits only change the database; Traefik Manager is written by
    `proxy push` or the dashboard's Save to YAML / Push now. Exits 1 when
    anything is pending, detached or waiting to be deleted (or Traefik Manager
    cannot be read), so a script can gate on it.
    """
    from orchestrator.registry.push import status_all

    configure_logging()
    build_engine()
    settings = get_settings()

    async def _go() -> Any:
        adapters = build_adapters(settings)
        await adapters.proxy.start()
        try:
            with Session(get_engine()) as session:
                (status,) = await status_all(session, adapters, ["proxy"])
                return status
        finally:
            await adapters.proxy.stop()

    status = asyncio.run(_go())
    _print_push_status(status)
    if status.total or not status.reachable:
        sys.exit(1)


@proxy.command("push")
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
@click.option("--hostname", "hostnames", multiple=True, help="Only these hostnames (repeatable).")
def proxy_push(yes: bool, hostnames: tuple[str, ...]) -> None:
    """Push the live DB's proxy hosts to Traefik Manager now.

    Same engine as the dashboard's Save to YAML / Push now, run in-process: it
    creates missing routes, applies edits, enables/disables, deletes routes of
    deleted hosts, and re-applies anything changed behind our back ("detached").
    One failing host does not stop the others; exits 1 if any item failed.
    services.yaml is NOT written (Save to YAML does that).
    """
    from orchestrator.registry.push import apply_all, status_all

    configure_logging()
    build_engine()
    settings = get_settings()
    only = list(hostnames) or None

    async def _go() -> list[Any]:
        adapters = build_adapters(settings)
        await adapters.proxy.start()
        try:
            with Session(get_engine()) as session:
                if not yes:
                    (status,) = await status_all(session, adapters, ["proxy"])
                    _print_push_status(status)
                    if not status.total:
                        click.echo("Nothing to push.")
                        return []
                    if not click.confirm("Push these changes to Traefik Manager?"):
                        raise click.Abort()
                results = await apply_all(
                    session, adapters, names=["proxy"], only=only, actor="cli"
                )
                session.commit()
                return results
        finally:
            await adapters.proxy.stop()

    if settings.dry_run:
        click.secho(
            "DRY_RUN is on - the dry-run proxy adapter is used; Traefik Manager is not contacted.",
            fg="yellow",
        )
    results = asyncio.run(_go())
    failed = 0
    for r in results:
        mark = click.style("ok  ", fg="green") if r.ok else click.style("FAIL", fg="red")
        click.echo(f"{mark} {r.hostname:<32} {r.action:<8} {r.message}")
        failed += 0 if r.ok else 1
    if results:
        click.secho(
            f"\n{len(results) - failed} done, {failed} failed.",
            fg="red" if failed else "green",
            bold=True,
        )
    if failed:
        sys.exit(1)


# ---------------------------------------------------------------------------
# service group
# ---------------------------------------------------------------------------


def _effective_requires(editor: RegistryEditor, svc: Any) -> list[str]:
    """Prerequisites of a service's backup: its policy's `requires`, then its own
    `backup_requires` (de-duplicated, in plan order)."""
    out: list[str] = []
    policy = editor.get_policy(str(svc.get("backup_policy"))) if svc.get("backup_policy") else None
    from_policy: list[Any] = list(policy.get("requires") or []) if policy is not None else []
    for slug in [*from_policy, *(svc.get("backup_requires") or [])]:
        if slug not in out:
            out.append(str(slug))
    return out


def _unwakeable_slugs(editor: RegistryEditor) -> set[str]:
    """Services whose node has no power target and is not always on — the same
    predicate the wake pipeline refuses on (`wake_is_unsupported`)."""
    nodes = {str(n.get("name")): n for n in editor.doc.get("nodes") or []}
    out: set[str] = set()
    for svc in editor.doc.get("services") or []:
        node = nodes.get(str(svc.get("node")))
        if node is None or (not node.get("always_on") and not node.get("power_mgr_target")):
            out.add(str(svc.get("slug")))
    return out


def _split_slugs(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.split(","):
        slug = part.strip()
        if slug and slug not in out:
            out.append(slug)
    return out


@click.group()
def service() -> None:
    """Add, update and remove services."""


@service.command("list")
@click.option("--file", "path_override", default=None)
def service_list(path_override: str | None) -> None:
    """Show every service in the file, with what would gate its backup."""
    editor = open_editor(path_override)
    try:
        editor.validate()
    except RegistryEditError as exc:
        # The orchestrator refuses a file like this at boot and registers nothing
        # while staying up, so say it here, where someone is looking.
        click.secho(f"{editor.path} will NOT load: {exc}", fg="red")
    services = editor.doc.get("services") or []
    if not services:
        click.echo("(no services)")
        return

    unwakeable = _unwakeable_slugs(editor)
    click.echo(f"{'SLUG':<24} {'NODE':<10} {'GUEST':<10} {'BACKUP':<10} {'PROBES':<14} NEEDS")
    for svc in services:
        probes = svc.get("probes") or []
        gating = sum(1 for p in probes if p.get("required", True))
        guest = f"{svc.get('guest_kind', '?')}/{svc.get('guest_id', '?')}"
        needs = _effective_requires(editor, svc)
        stuck = [n for n in needs if n in unwakeable]
        if svc.get("backup_excluded"):
            backup, colour = "EXCLUDED", "yellow"
        elif gating == 0:
            backup, colour = "blocked", "yellow"  # no gating probes -> UNKNOWN
        elif stuck:
            backup, colour = "blocked", "red"  # a prerequisite can never be woken
        else:
            backup, colour = "eligible", None
        probe_col = f"{len(probes)} ({gating} gating)"
        needs_col = ",".join(needs) if needs else "-"
        if stuck:
            needs_col += f"  (cannot wake: {','.join(stuck)} - no power target)"
        click.secho(
            f"{svc.get('slug', '?'):<24} {svc.get('node', '?'):<10} {guest:<10} "
            f"{backup:<10} {probe_col:<14} {needs_col}",
            fg=colour,
        )


@service.command("update")
@click.argument("slug")
@click.option("--enabled/--disabled", default=None, help="Manage this service at all?")
@click.option(
    "--exclude-backup/--include-backup",
    "backup_excluded",
    default=None,
    help="Hard 'never back this up' — for a VM hosting PBS's own storage.",
)
@click.option("--reason", default=None, help="Why it is excluded.")
@click.option("--policy", "backup_policy", default=None, help="Backup policy name.")
@click.option(
    "--depends-on",
    default=None,
    help=(
        "Comma-separated slugs that must be HEALTHY first (e.g. the gateway). "
        "Pass '' to clear. A blocked dependency makes this service UNKNOWN, "
        "never FAILED."
    ),
)
@click.option(
    "--backup-requires",
    default=None,
    help=(
        "Comma-separated slugs that must be UP (woken and held) before THIS service's "
        "backup, on top of its policy's `requires` - e.g. its database service. "
        "Pass '' to clear. Not the same as --depends-on (a health-gate dependency)."
    ),
)
@click.option("--name", default=None)
@click.option("--description", default=None)
@click.option(
    "--group",
    default=None,
    help="Purely organisational tag the dashboard groups services by. Pass '' to clear.",
)
@click.option(
    "--ip",
    "ip_address",
    default=None,
    help=(
        "Manual IP address (wins over a discovered one; used for proxy upstreams and "
        "ping probes). Pass '' to clear and fall back to discovery."
    ),
)
@click.option("--file", "path_override", default=None)
def service_update(
    slug: str,
    enabled: bool | None,
    backup_excluded: bool | None,
    reason: str | None,
    backup_policy: str | None,
    depends_on: str | None,
    backup_requires: str | None,
    name: str | None,
    description: str | None,
    group: str | None,
    ip_address: str | None,
    path_override: str | None,
) -> None:
    """Update fields on SLUG. Only the flags you pass are touched."""
    editor = open_editor(path_override)
    editor.require_service(slug)

    fields: dict[str, Any] = {}
    if enabled is not None:
        fields["enabled"] = enabled
    if backup_excluded is not None:
        fields["backup_excluded"] = backup_excluded
        if backup_excluded and not reason:
            reason = click.prompt(
                "  Why is this excluded? (recorded in the refusal message)",
                default="hosts storage the PBS datastore lives on",
                type=str,
            )
    if reason is not None:
        fields["backup_excluded_reason"] = reason
    if backup_policy is not None:
        fields["backup_policy"] = backup_policy
    if depends_on is not None:
        fields["depends_on"] = [d.strip() for d in depends_on.split(",") if d.strip()]
    if backup_requires is not None:
        fields["backup_requires"] = _split_slugs(backup_requires)
    if name is not None:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if group is not None:
        fields["group"] = group
    if ip_address is not None:
        try:
            fields["ip_address"] = validate_ip_address(ip_address)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--ip") from None

    if not fields:
        raise click.UsageError("nothing to change — pass at least one option")

    editor.upsert_service(slug, fields)
    save(editor, f"service {slug!r} updated ({', '.join(sorted(fields))})")


# ---------------------------------------------------------------------------
# policy group
# ---------------------------------------------------------------------------


@click.group()
def policy() -> None:
    """Show and edit backup policies' wake settings."""


@policy.command("list")
@click.option("--file", "path_override", default=None)
def policy_list(path_override: str | None) -> None:
    """Show every backup policy: schedule, what it requires, whether it wakes targets."""
    editor = open_editor(path_override)
    try:
        editor.validate()
    except RegistryEditError as exc:
        click.secho(f"{editor.path} will NOT load: {exc}", fg="red")
    policies = editor.doc.get("backup_policies") or []
    if not policies:
        click.echo("(no backup policies)")
        return
    unwakeable = _unwakeable_slugs(editor)
    click.echo(f"{'POLICY':<22} {'SCHEDULE':<14} {'WAKE TARGETS':<13} {'REQUIRES':<24} SERVICES")
    for pol in policies:
        name = str(pol.get("name"))
        requires = [str(r) for r in pol.get("requires") or []]
        members = [
            str(s.get("slug"))
            for s in editor.doc.get("services") or []
            if s.get("backup_policy") == name
        ]
        stuck = [r for r in requires if r in unwakeable]
        req_col = ",".join(requires) if requires else "-"
        if stuck:
            req_col += f"  (cannot wake: {','.join(stuck)})"
        click.secho(
            f"{name:<22} {(pol.get('schedule_cron') or 'manual'):<14} "
            f"{'yes' if pol.get('wake_before_backup', True) else 'no':<13} {req_col:<24} "
            f"{','.join(members) or '-'}",
            fg="red" if stuck else None,
        )


@policy.command("update")
@click.argument("name")
@click.option(
    "--requires",
    default=None,
    help=(
        "Comma-separated service slugs that must be UP before any backup under this "
        "policy (e.g. the TrueNAS VM hosting the PBS datastore's NFS). They are woken "
        "first and held until the backup finishes. Pass '' to clear."
    ),
)
@click.option("--clear-requires", is_flag=True, help="Same as --requires ''.")
@click.option(
    "--wake-before-backup/--no-wake-before-backup",
    default=None,
    help="Wake the services this policy backs up (node on, guest running) before each backup.",
)
@click.option("--file", "path_override", default=None)
def policy_update(
    name: str,
    requires: str | None,
    clear_requires: bool,
    wake_before_backup: bool | None,
    path_override: str | None,
) -> None:
    """Update a policy's wake settings. Only the flags you pass are touched."""
    if requires is not None and clear_requires:
        raise click.UsageError("--requires and --clear-requires are mutually exclusive")
    editor = open_editor(path_override)
    try:
        editor.require_policy(name)
    except RegistryEditError as exc:
        raise click.ClickException(str(exc)) from exc

    fields: dict[str, Any] = {}
    if clear_requires:
        fields["requires"] = []
    elif requires is not None:
        fields["requires"] = _split_slugs(requires)
    if wake_before_backup is not None:
        fields["wake_before_backup"] = wake_before_backup
    if not fields:
        raise click.UsageError("nothing to change — pass at least one option")

    editor.update_policy(name, fields)
    save(editor, f"policy {name!r} updated ({', '.join(sorted(fields))})")


@service.command("remove")
@click.argument("slug")
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
@click.option("--file", "path_override", default=None)
def service_remove(slug: str, yes: bool, path_override: str | None) -> None:
    """Remove SLUG from the registry."""
    editor = open_editor(path_override)
    editor.require_service(slug)
    if not yes and not click.confirm(f"Remove {slug!r} from {editor.path}?"):
        click.echo("aborted")
        return
    editor.remove_service(slug)
    save(editor, f"service {slug!r} removed")
    click.secho(
        "Backup history for this service is preserved in the database.",
        fg="cyan",
    )
