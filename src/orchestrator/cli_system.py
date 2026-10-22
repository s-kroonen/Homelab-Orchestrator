"""``orchestrator-cli system`` - the dashboard's System page, from the shell.

Same functions (``orchestrator.system``) as the page and its API. The shell
version works on the files directly, so it also works while the orchestrator
is stopped (a restore staged here is applied on the next start).
"""

from __future__ import annotations

import asyncio
import json

import click

from orchestrator.config import get_settings
from orchestrator.system import backups as bk
from orchestrator.system import info as sysinfo
from orchestrator.system import update as upd


def _size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return str(n)


@click.group()
def system() -> None:
    """The orchestrator itself: version, backups, restore, schema, update."""


@system.command("info")
def system_info() -> None:
    """Version, build, database, schema and backup settings."""
    info = sysinfo.system_info(get_settings())
    schema = info["schema"]
    click.echo(f"version   : {info['version']} {info['git_sha'][:7]}".rstrip())
    click.echo(f"image     : {info['image'] or '(not reported)'}")
    click.echo(f"database  : {info['database']}")
    click.echo(f"schema    : {schema['current'] or 'none'} (newest {schema['head']})")
    click.echo(f"registry  : {info['registry']}")
    if schema["pending"]:
        click.secho("The database needs upgrading: orchestrator-cli system migrate", fg="yellow")


@system.command("backup")
@click.option("--note", default="", help="A note stored with the backup.")
def system_backup(note: str) -> None:
    """Back up the database and services.yaml now."""
    try:
        info = bk.create_backup(get_settings(), reason="manual", note=note)
    except bk.BackupError as exc:
        raise click.ClickException(str(exc)) from exc
    click.secho(f"Saved {info.name} ({_size(info.size_bytes)})", fg="green")


@system.command("backups")
def system_backups() -> None:
    """List the backups, newest first."""
    rows = bk.list_backups(get_settings())
    if not rows:
        click.echo("No backups yet.")
    for b in rows:
        click.echo(f"{b.name:<58} {_size(b.size_bytes):>9}  {b.reason:<14} {b.note}")


@system.command("restore")
@click.argument("name")
def system_restore(name: str) -> None:
    """Stage restoring NAME; it is applied the next time the orchestrator starts."""
    settings = get_settings()
    try:
        marker = bk.stage_restore(settings, name, known_revisions=sysinfo.known_revisions(settings))
    except bk.BackupError as exc:
        raise click.ClickException(str(exc)) from exc
    click.secho(f"Staged {marker['backup']}. Restart the orchestrator to apply it.", fg="yellow")


@system.command("restore-cancel")
def system_restore_cancel() -> None:
    """Drop a staged restore."""
    click.echo("Cancelled." if bk.cancel_restore(get_settings()) else "Nothing was staged.")


@system.command("migrate")
def system_migrate() -> None:
    """Back up, then upgrade the database schema to the newest this version knows."""
    from alembic import command

    settings = get_settings()
    status = sysinfo.schema_status(settings)
    if not status["pending"]:
        click.echo("Already up to date.")
        return
    try:
        info = bk.create_backup(settings, reason="pre-migration")
        click.echo(f"Backed up to {info.name}")
    except bk.BackupError as exc:
        click.secho(f"No backup taken: {exc}", fg="yellow")
    command.upgrade(sysinfo._alembic_config(settings), "head")
    click.secho("Database upgraded.", fg="green")


@system.command("update-check")
@click.option("--json", "as_json", is_flag=True)
def system_update_check(as_json: bool) -> None:
    """Is there a newer build than this one?"""
    check = asyncio.run(sysinfo.check_update(get_settings(), force=True))
    if as_json:
        click.echo(json.dumps(check.as_dict(), indent=2))
        return
    colour = "yellow" if check.available else None
    click.secho(check.message, fg=colour)


@system.command("update")
def system_update() -> None:
    """Back up, then run SELF_UPDATE_COMMAND (or print the manual steps)."""
    result = asyncio.run(upd.run_update(get_settings()))
    if result.backup:
        click.echo(f"Backed up to {result.backup.name}")
    if result.backup_error:
        click.secho(f"No backup taken: {result.backup_error}", fg="yellow")
    click.echo(result.message)
    if not result.ran:
        for step in result.manual_steps:
            click.echo(f"  {step}")
        return
    if result.output:
        click.echo(result.output)
    if not result.ok:
        raise SystemExit(1)
