"""``orchestrator-cli dump`` - dump one database on its own: configure, run, list, fetch.

The same settings and files as the service page's Dumps section. Configuration is edited in the
registry file like every other service setting; running a dump acts on the live system (the
database's dump tool runs inside its container through Portainer). See docs/dumps.md.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import click

from orchestrator.cli_config import open_editor, save
from orchestrator.dumps.policy import TYPES, check_target, describe, normalize_dump


@click.group()
def dump() -> None:
    """Dumps of a service's own database: configure, run, list."""


def _current(slug: str, path_override: str | None) -> tuple[Any, dict[str, Any]]:
    editor = open_editor(path_override)
    editor.require_service(slug)
    svc: dict[str, Any] = editor.get_service(slug) or {}
    return editor, json.loads(json.dumps(svc.get("dump") or {}, default=dict))


@dump.command("show")
@click.argument("slug")
@click.option("--file", "path_override", default=None)
def dump_show(slug: str, path_override: str | None) -> None:
    """How SLUG is dumped."""
    _, cfg = _current(slug, path_override)
    for line in describe(normalize_dump(cfg)):
        click.echo(f"  {line}")


@dump.command("set")
@click.argument("slug")
@click.option("--type", "kind", type=click.Choice(list(TYPES)), default=None)
@click.option("--database", default=None, help="One database ('' = every database).")
@click.option("--user", default=None, help="Database user (default: the container's own).")
@click.option("--path", default=None, help="sqlite: the database file inside the container.")
@click.option(
    "--command", default=None, help="type command: runs in the container, stdout is the dump."
)
@click.option("--container", default=None, help="Container to run in (default: the service's own).")
@click.option("--schedule", default=None, help="Cron expression ('' = only when run by hand).")
@click.option("--keep", type=int, default=None, help="Keep the newest N dumps.")
@click.option("--keep-days", type=int, default=None, help="Also drop dumps older than N days.")
@click.option("--enable/--disable", "enabled", default=None)
@click.option("--file", "path_override", default=None)
def dump_set(
    slug: str,
    kind: str | None,
    database: str | None,
    user: str | None,
    path: str | None,
    command: str | None,
    container: str | None,
    schedule: str | None,
    keep: int | None,
    keep_days: int | None,
    enabled: bool | None,
    path_override: str | None,
) -> None:
    """Set SLUG's dump. Only the options you pass change."""
    editor, cur = _current(slug, path_override)
    svc = editor.get_service(slug) or {}
    cfg = dict(cur or {"type": kind or "postgres"})
    for key, value in (
        ("type", kind),
        ("database", database),
        ("user", user),
        ("path", path),
        ("command", command),
        ("container", container),
        ("schedule", schedule),
        ("keep", keep),
        ("keep_days", keep_days),
        ("enabled", enabled),
    ):
        if value is not None:
            cfg[key] = value
    try:
        cfg = normalize_dump(cfg)
        check_target(cfg, svc.get("controller") or {})
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    editor.upsert_service(slug, {"dump": cfg})
    save(editor, f"dump settings for {slug!r}")
    for line in describe(cfg):
        click.echo(f"  {line}")


@dump.command("clear")
@click.argument("slug")
@click.option("--file", "path_override", default=None)
def dump_clear(slug: str, path_override: str | None) -> None:
    """Remove SLUG's dump settings (files already taken are kept)."""
    editor, _ = _current(slug, path_override)
    (editor.get_service(slug) or {}).pop("dump", None)
    save(editor, f"dump settings removed from {slug!r}")


@dump.command("run")
@click.argument("slug")
def dump_run(slug: str) -> None:
    """Dump SLUG now and wait for the result."""
    from sqlmodel import Session

    from orchestrator.adapters.factory import build_adapters
    from orchestrator.config import get_settings
    from orchestrator.db.session import build_engine, get_engine
    from orchestrator.domain.enums import PipelineStatus
    from orchestrator.dumps.pipeline import DumpPipeline, DumpRefused
    from orchestrator.logging_setup import configure_logging

    configure_logging()
    build_engine()
    settings = get_settings()
    adapters = build_adapters(settings)
    if settings.dry_run:
        click.secho("DRY_RUN is on - nothing real is dumped.", fg="yellow")

    async def go() -> Any:
        await adapters.start_all()
        try:
            with Session(get_engine()) as session:
                run = await DumpPipeline(adapters, settings).run_for_service(session, slug, "cli")
                session.refresh(run)
                session.expunge(run)
                return run
        finally:
            await adapters.stop_all()

    try:
        run = asyncio.run(go())
    except DumpRefused as exc:
        raise click.ClickException(str(exc)) from exc
    colour = "green" if run.status is PipelineStatus.SUCCEEDED else "red"
    click.secho(f"Run #{run.id} - {run.status.value}", fg=colour, bold=True)
    for step in run.steps:
        note = (
            (step.get("detail") or {}).get("error") or (step.get("detail") or {}).get("path") or ""
        )
        click.echo(f"  {step['status']:<8} {step['name']:<10} {note}")
    if run.error:
        click.secho(f"  {run.error}", fg="red")
    if run.status is not PipelineStatus.SUCCEEDED:
        raise SystemExit(1)


@dump.command("list")
@click.argument("slug", required=False)
@click.option("--json", "as_json", is_flag=True)
def dump_list(slug: str | None, as_json: bool) -> None:
    """The dump files kept (of SLUG, or every service)."""
    from sqlmodel import Session, col, select

    from orchestrator.config import get_settings
    from orchestrator.db.models import DumpRecord
    from orchestrator.db.session import build_engine, get_engine
    from orchestrator.dumps.pipeline import dump_root
    from orchestrator.logging_setup import configure_logging

    configure_logging()
    build_engine()
    root = dump_root(get_settings())
    with Session(get_engine()) as session:
        stmt = select(DumpRecord).order_by(col(DumpRecord.created_at).desc())
        if slug:
            stmt = stmt.where(DumpRecord.service_slug == slug)
        rows = session.exec(stmt).all()
        out = [
            {
                "id": r.id,
                "service": r.service_slug,
                "file": str(root / r.path),
                "size_bytes": r.size_bytes,
                "verified": r.verified,
                "protected": r.protected,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    if not out:
        click.echo("No dumps.")
    for r in out:
        flags = ("" if r["verified"] else " UNCHECKED") + (" protected" if r["protected"] else "")
        click.echo(f"#{r['id']:<4} {r['service']:<20} {r['size_bytes']:>12,} B  {r['file']}{flags}")
