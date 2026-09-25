"""Interactive config builders — the `probe`, `proxy` and `service` CLI groups.

Goal: an operator should never have to hand-edit ``services.yaml``. Everything
these commands write is validated before it touches disk, and every edit is
surgical, so existing comments and hand-tuning survive.

Every prompt-driven command also accepts flags, so the same operations work
non-interactively from a script or an Ansible task.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from orchestrator.config import get_settings
from orchestrator.domain.enums import ProbeKind
from orchestrator.registry.editor import RegistryEditError, RegistryEditor
from orchestrator.registry.probe_specs import (
    PROBE_SPECS,
    PROXY_FIELDS,
    TRANSPORT_FIELDS,
    FieldSpec,
    kinds_by_menu_order,
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


@proxy.command("add")
@click.argument("slug")
@click.option("--hostname", default=None)
@click.option("--upstream", default=None)
@click.option("--provider", "router_provider", default=None)
@click.option("--file", "path_override", default=None)
@click.option("--non-interactive", is_flag=True, help="Never prompt. For scripts.")
def proxy_add(
    slug: str,
    hostname: str | None,
    upstream: str | None,
    router_provider: str | None,
    path_override: str | None,
    non_interactive: bool,
) -> None:
    """Attach a proxy host to SLUG."""
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
            click.echo(f"    {h.get('hostname'):<34} -> {h.get('upstream'):<28} [{provider}]")
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


# ---------------------------------------------------------------------------
# service group
# ---------------------------------------------------------------------------


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

    click.echo(f"{'SLUG':<24} {'NODE':<10} {'GUEST':<10} {'BACKUP':<10} PROBES")
    for svc in services:
        probes = svc.get("probes") or []
        gating = sum(1 for p in probes if p.get("required", True))
        guest = f"{svc.get('guest_kind', '?')}/{svc.get('guest_id', '?')}"
        if svc.get("backup_excluded"):
            backup, colour = "EXCLUDED", "yellow"
        elif gating == 0:
            backup, colour = "blocked", "yellow"  # no gating probes -> UNKNOWN
        else:
            backup, colour = "eligible", None
        click.secho(
            f"{svc.get('slug', '?'):<24} {svc.get('node', '?'):<10} {guest:<10} "
            f"{backup:<10} {len(probes)} ({gating} gating)",
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
@click.option("--name", default=None)
@click.option("--description", default=None)
@click.option("--file", "path_override", default=None)
def service_update(
    slug: str,
    enabled: bool | None,
    backup_excluded: bool | None,
    reason: str | None,
    backup_policy: str | None,
    depends_on: str | None,
    name: str | None,
    description: str | None,
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
    if name is not None:
        fields["name"] = name
    if description is not None:
        fields["description"] = description

    if not fields:
        raise click.UsageError("nothing to change — pass at least one option")

    editor.upsert_service(slug, fields)
    save(editor, f"service {slug!r} updated ({', '.join(sorted(fields))})")


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
