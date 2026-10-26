"""``orchestrator-cli archive`` - deleted services, proxy hosts and policies waiting out their 30 days.

The same list, restore and purge as the Archive page. Works on the live database, so run it
where the orchestrator's database is (inside the container). See docs/archive.md.
"""

from __future__ import annotations

import json

import click


def _open() -> None:
    from orchestrator.db.session import build_engine
    from orchestrator.logging_setup import configure_logging

    configure_logging()
    build_engine()


@click.group()
def archive() -> None:
    """Deleted services, proxy hosts and backup policies: list, restore, purge."""


@archive.command("list")
@click.option("--json", "as_json", is_flag=True)
def archive_list(as_json: bool) -> None:
    """What is in the archive, newest first."""
    from sqlmodel import Session

    from orchestrator.db.session import get_engine
    from orchestrator.registry import archive as arch

    _open()
    with Session(get_engine()) as session:
        arch.purge_expired(session)
        rows = [arch.describe(r) for r in arch.list_archive(session)]
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("The archive is empty.")
    for r in rows:
        extra = f" (+{len(r['sub_services'])} sub-services)" if r["sub_services"] else ""
        click.echo(
            f"  #{r['id']:<4} {r['kind_label'].lower():<13} {r['slug']}{extra}  deleted {r['deleted_at'][:16]} "
            f"by {r['deleted_by'] or '?'}  purged in {r['days_left']}d"
        )


def _pick(session: object, ref: str) -> int:
    """An archive id, or a name (the newest entry of any kind with it)."""
    from sqlmodel import Session

    from orchestrator.registry import archive as arch

    assert isinstance(session, Session)
    if ref.isdigit():
        return int(ref)
    row = next((r for r in arch.list_archive(session) if r.slug == ref), None) or arch.latest_for(
        session, ref
    )
    if row is None or row.id is None:
        raise click.ClickException(f"nothing in the archive for {ref!r}")
    return row.id


@archive.command("restore")
@click.argument("ref")
def archive_restore(ref: str) -> None:
    """Bring REF (an archive id or a name) back. Proxy hosts return un-pushed."""
    from sqlmodel import Session

    from orchestrator.db.session import get_engine
    from orchestrator.registry import archive as arch

    _open()
    with Session(get_engine()) as session:
        archive_id = _pick(session, ref)
        entry = session.get(arch.ArchivedService, archive_id)
        kind = entry.kind if entry else ""
        try:
            result = arch.restore(session, archive_id, "cli")
        except arch.ArchiveError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"Restored {', '.join(result.restored)}.")
    if kind == arch.KIND_POLICY:
        click.echo(
            "  note: restart the orchestrator (or save the policy on its page) so its schedule runs."
        )
    for warning in result.warnings:
        click.echo(f"  note: {warning}")
    click.echo("Save the registry from the dashboard (Save to YAML) to keep it.")


@archive.command("purge")
@click.argument("ref")
@click.option("--yes", is_flag=True, help="Do not ask.")
def archive_purge(ref: str, yes: bool) -> None:
    """Delete REF (an archive id or a name) for good."""
    from sqlmodel import Session

    from orchestrator.db.session import get_engine
    from orchestrator.registry import archive as arch

    _open()
    with Session(get_engine()) as session:
        archive_id = _pick(session, ref)
        if not yes:
            click.confirm(f"Delete archive entry #{archive_id} for good?", abort=True)
        try:
            slug = arch.purge_now(session, archive_id, "cli")
        except arch.ArchiveError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"Purged {slug}.")
