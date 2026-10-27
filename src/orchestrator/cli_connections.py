"""``orchestrator-cli connections`` - the same connections as the dashboard's Connections page.

It edits ``connections.yaml`` directly (run it where the state directory is, i.e. inside the
container); a running orchestrator notices the change within a few seconds. Secrets are never
printed. See docs/connections.md.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from orchestrator.connections import service
from orchestrator.connections.kinds import BY_KIND, KINDS


def _settings() -> Any:
    from orchestrator.config import get_settings, reload_settings
    from orchestrator.logging_setup import configure_logging

    configure_logging()
    get_settings()
    return reload_settings()  # what the file says now, not what an earlier call in this process saw


def _session() -> Any:
    from sqlmodel import Session

    from orchestrator.db.session import build_engine, get_engine

    build_engine()
    return Session(get_engine())


def _run(fn: Any, *args: Any, **kw: Any) -> Any:
    try:
        return fn(*args, **kw)
    except service.Refused as exc:
        raise click.ClickException(str(exc)) from exc


def _pairs(items: tuple[str, ...], what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise click.ClickException(f"{what} {item!r}: write it as name=value")
        out[key.strip()] = value
    return out


def _split(kind: str, values: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Plain and secret fields of ``kind`` from one name=value list."""
    spec = BY_KIND.get(kind)
    if spec is None:
        raise click.ClickException(f"unknown kind {kind!r}; one of: {', '.join(BY_KIND)}")
    secret = {f.name for f in spec.fields if f.is_secret}
    known = {f.name for f in spec.fields}
    for key in values:
        if key not in known:
            raise click.ClickException(
                f"{kind} has no field {key!r}; it has: {', '.join(sorted(known))}"
            )
    return (
        {k: v for k, v in values.items() if k not in secret},
        {k: v for k, v in values.items() if k in secret},
    )


@click.group()
def connections() -> None:
    """Adapters and controllers: Proxmox, PBS, Traefik Manager, Portainer(s), MQTT, alerts, SSH."""


@connections.command("kinds")
def connections_kinds() -> None:
    """The kinds that can be added and their fields."""
    for spec in KINDS:
        click.echo(f"{spec.kind:<12} {spec.title}{'  (several allowed)' if spec.multi else ''}")
        for f in spec.fields:
            flags = ("required " if f.required else "") + ("secret" if f.is_secret else "")
            click.echo(f"    {f.name:<22} {f.label}  {flags.strip()}")


@connections.command("list")
@click.option("--json", "as_json", is_flag=True)
def connections_list(as_json: bool) -> None:
    """Every connection, from the file and from .env."""
    from orchestrator.connections import runtime

    _settings()
    snap = runtime.current()
    rows = [c.public() for c in snap.all]
    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
        return
    click.echo(f"file: {snap.path}" + (f"  (UNUSABLE: {snap.error})" if snap.error else ""))
    if not rows:
        click.echo("No connections.")
    for r in rows:
        state = "off" if not r["enabled"] else "on" if r["configured"] else "incomplete"
        tags = " ".join(t for t in (r["site"], r["scope"] if r["scope"] == "shared" else "") if t)
        flag = "*" if r["default"] else " "
        click.echo(f" {flag} {r['id']:<18} {r['kind']:<12} {state:<10} {r['source']:<5} {tags}")
    click.echo("(* = default of its kind; source env = still in .env, see `import-env`)")


@connections.command("show")
@click.argument("ident")
def connections_show(ident: str) -> None:
    """One connection's values (secrets only as set / not set)."""
    from orchestrator.connections import runtime

    _settings()
    conn = runtime.current().get(ident)
    if conn is None:
        raise click.ClickException(f"no connection called {ident!r}")
    click.echo(json.dumps(conn.public(), indent=2, default=str))


@connections.command("add")
@click.argument("kind")
@click.option("--id", "ident", default="", help="Default: made from --name.")
@click.option("--name", default="")
@click.option("--set", "values", multiple=True, help="field=value (repeatable); secrets too.")
@click.option("--site", default="")
@click.option("--shared", is_flag=True, help="Mark it as shared with other orchestrators.")
@click.option("--default", "make_default", is_flag=True)
@click.option("--disabled", is_flag=True)
@click.option("--notes", default="")
def connections_add(
    kind: str,
    ident: str,
    name: str,
    values: tuple[str, ...],
    site: str,
    shared: bool,
    make_default: bool,
    disabled: bool,
    notes: str,
) -> None:
    """Add a connection: `connections add portainer --id offsite --set url=https://… --set api_key=…`."""
    settings = _settings()
    plain, secret = _split(kind, _pairs(values, "--set"))
    change = _run(
        service.create,
        settings,
        kind=kind,
        ident=ident,
        name=name,
        enabled=not disabled,
        default=make_default,
        site=site or settings.orchestrator_site,
        scope="shared" if shared else "local",
        notes=notes,
        settings_in=plain,
        secrets_in=secret,
    )
    assert change.connection is not None
    click.echo(f"Added {change.connection.id}.")


@connections.command("set")
@click.argument("ident")
@click.option(
    "--set", "values", multiple=True, help="field=value (repeatable); a blank secret keeps."
)
@click.option("--clear", "clear", multiple=True, help="Remove this secret field.")
@click.option("--name", default=None)
@click.option("--site", default=None)
@click.option("--scope", type=click.Choice(["local", "shared"]), default=None)
@click.option("--notes", default=None)
@click.option("--enable/--disable", "enabled", default=None)
def connections_set(
    ident: str,
    values: tuple[str, ...],
    clear: tuple[str, ...],
    name: str | None,
    site: str | None,
    scope: str | None,
    notes: str | None,
    enabled: bool | None,
) -> None:
    """Change a connection."""
    settings = _settings()
    kind = _kind_of(ident)
    plain, secret = _split(kind, _pairs(values, "--set"))
    _run(
        service.update,
        settings,
        ident,
        name=name,
        enabled=enabled,
        site=site,
        scope=scope,
        notes=notes,
        settings_in=plain,
        secrets_in=secret,
        clear_secrets=list(clear),
    )
    click.echo(f"Updated {ident}.")


def _kind_of(ident: str) -> str:
    from orchestrator.connections import runtime

    conn = runtime.current().get(ident)
    if conn is None:
        raise click.ClickException(f"no connection called {ident!r}")
    return conn.kind


@connections.command("remove")
@click.argument("ident")
@click.option("--force", is_flag=True, help="Even if services still use it.")
@click.option("--yes", is_flag=True)
def connections_remove(ident: str, force: bool, yes: bool) -> None:
    """Delete a connection."""
    settings = _settings()
    if not yes:
        click.confirm(f"Delete the connection {ident}?", abort=True)
    with _session() as session:
        _run(service.delete, session, settings, ident, force=force)
    click.echo(f"Deleted {ident}.")


@connections.command("default")
@click.argument("ident")
def connections_default(ident: str) -> None:
    """Make IDENT the default of its kind (what a link that names none means)."""
    settings = _settings()
    with _session() as session:
        change = _run(service.set_default, session, settings, ident)
        session.commit()
    click.echo(f"{ident} is now the default.")
    if change.pinned:
        click.echo(
            f"  {', '.join(change.pinned)} were pinned to the previous default; "
            "save the registry (Save to YAML on the dashboard) to keep that."
        )


@connections.command("test")
@click.argument("ident")
def connections_test(ident: str) -> None:
    """Look at a connection (read-only)."""
    import asyncio

    from orchestrator.connections import runtime
    from orchestrator.connections.checks import check_connection

    settings = _settings()
    conn = runtime.current().get(ident)
    if conn is None:
        raise click.ClickException(f"no connection called {ident!r}")
    result = asyncio.run(check_connection(conn, settings))
    mark = {True: "ok", False: "FAILED", None: "-"}[result.ok]
    click.echo(f"{ident}: {mark} {result.detail}")
    if result.ok is False:
        sys.exit(1)


@connections.command("import-env")
def connections_import_env() -> None:
    """Move what .env configures into the connections file."""
    settings = _settings()
    moved, keys = _run(service.import_env, settings)
    if not moved:
        click.echo("Nothing to move: every connection is already in the file.")
        return
    click.echo(f"Moved: {', '.join(c.id for c in moved)}.")
    click.echo("These .env keys are ignored from now on - delete them from .env:")
    click.echo("  " + "\n  ".join(keys))


@connections.command("export")
@click.option("--with-secrets", is_flag=True)
@click.option("--all", "everything", is_flag=True, help="Local connections too.")
def connections_export(with_secrets: bool, everything: bool) -> None:
    """Print the shared connections for another orchestrator."""
    click.echo(
        _run(
            service.export_shared,
            _settings(),
            with_secrets=with_secrets,
            scope="all" if everything else "shared",
        ),
        nl=False,
    )


@connections.command("import")
@click.argument("file", type=click.File("r"))
def connections_import(file: Any) -> None:
    """Add the connections of an export (FILE, or - for stdin); existing ids are left alone."""
    added, skipped = _run(service.import_shared, _settings(), file.read())
    click.echo(
        f"Added: {', '.join(added) or 'nothing'}. Already here: {', '.join(skipped) or 'none'}."
    )
    if added:
        click.echo("They arrive switched off: add the secrets, then `connections set ID --enable`.")
