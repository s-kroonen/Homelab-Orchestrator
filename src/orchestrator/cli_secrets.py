"""``orchestrator-cli secrets``: the master key and the credentials it encrypts (OR-70).

    secrets status        key id and where it is, how many secrets are encrypted, any still plain
    secrets encrypt       encrypt every stored secret now (the orchestrator also does this at start)
    secrets decrypt       write them back as plain text, before downgrading to an older version
    secrets rotate        a new master key, everything re-encrypted with it
    secrets export-key    print the master key once, to keep in a password manager

The key itself is only ever printed by ``export-key``, and only after a confirmation.
"""

from __future__ import annotations

import base64

import click
from sqlmodel import Session

from orchestrator import vault
from orchestrator.audit import log as audit
from orchestrator.config import get_settings
from orchestrator.db.session import build_engine, get_engine
from orchestrator.logging_setup import configure_logging


def _open() -> None:
    configure_logging()
    build_engine()
    try:
        vault.boot(get_settings())
    except vault.VaultError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def secrets() -> None:
    """The master key and the credentials it encrypts."""


@secrets.command("status")
def status() -> None:
    """Which key is in use and whether anything is still stored in plain text."""
    _open()
    with Session(get_engine()) as session:
        report = vault.status(get_settings(), session)
    click.echo(f"Master key   {report.key_id}  ({report.key_path})")
    click.echo(
        f"Connections  {report.connections_encrypted} encrypted, "
        f"{report.connections_plain} plain"
    )
    click.echo(f"Database     {report.db_encrypted} encrypted, {report.db_plain} plain")
    if report.plain:
        click.secho(
            "Some secrets are still plain text: orchestrator-cli secrets encrypt", fg="yellow"
        )
    else:
        click.secho("Every stored secret is encrypted.", fg="green")


@secrets.command("encrypt")
def encrypt() -> None:
    """Encrypt every stored secret now (connections.yaml and its backups, the database)."""
    _open()
    with Session(get_engine()) as session:
        report = vault.encrypt_everything(get_settings(), session)
        if report.changed:
            audit.record(
                session,
                actor="cli",
                action="vault.encrypted",
                details={"key_id": report.key_id, "backups": report.backups_rewritten},
            )
            session.commit()
    for problem in report.backups_unreadable:
        click.secho(f"Left as it is (unreadable): {problem}", fg="yellow")
    click.secho(
        (
            "Done: everything is encrypted with key " + report.key_id
            if report.changed
            else "Nothing to do: everything was already encrypted with key " + report.key_id
        ),
        fg="green",
    )


@secrets.command("decrypt")
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
def decrypt(yes: bool) -> None:
    """Write every secret back as plain text, before going back to an older version.

    Versions from before encryption read an encrypted value as the credential itself, so every
    connection would fail. Run this, then downgrade. The key stays; this version encrypts
    everything again a few minutes after its next start.
    """
    _open()
    if not yes and not click.confirm(
        "Write every stored secret back as plain text (for a downgrade)?"
    ):
        click.echo("Aborted.")
        return
    with Session(get_engine()) as session:
        changed = vault.decrypt_everything(get_settings(), session)
        audit.record(session, actor="cli", action="vault.decrypted", details={"values": changed})
        session.commit()
    click.secho(f"{changed} secret(s) are plain text again. Downgrade now.", fg="yellow")


@secrets.command("rotate")
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
def rotate(yes: bool) -> None:
    """Make a new master key and re-encrypt every secret with it.

    Afterwards the old key no longer opens anything: export the new one (secrets export-key)
    and replace the copy in your password manager. Sessions signed with a key derived from the
    master key end, so everyone signs in again.
    """
    _open()
    if not yes and not click.confirm("Make a new master key and re-encrypt everything?"):
        click.echo("Aborted.")
        return
    with Session(get_engine()) as session:
        try:
            old, new = vault.rotate(get_settings(), session)
        except vault.VaultError as exc:
            raise click.ClickException(str(exc)) from exc
        audit.record(session, actor="cli", action="vault.rotated", details={"old": old, "new": new})
        session.commit()
    click.secho(f"Rotated: key {old} is retired, key {new} is in use.", fg="green")
    click.echo("Now run: orchestrator-cli secrets export-key, and replace your saved copy.")


@secrets.command("export-key")
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
def export_key(yes: bool) -> None:
    """Print the master key, to keep in a password manager.

    Anyone with this key and a copy of the data can read every credential. Without it, a
    backup restored on another host cannot.
    """
    _open()
    if not yes and not click.confirm(
        "Print the master key? Anyone who sees it can decrypt every stored credential."
    ):
        click.echo("Aborted.")
        return
    key = vault.current().key
    with Session(get_engine()) as session:
        audit.record(session, actor="cli", action="vault.key_exported", details={"key_id": key.id})
        session.commit()
    click.echo(f"Key {key.id}:")
    click.echo(base64.b64encode(key.raw).decode())
    click.echo(
        "Store it in a password manager. To use it on another host, write it to a file and "
        "set ORCHESTRATOR_MASTER_KEY_FILE to that file."
    )


__all__ = ["secrets"]
