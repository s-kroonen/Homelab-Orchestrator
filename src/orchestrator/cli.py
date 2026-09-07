"""Operator CLI — the fastest way to test connectivity and run a backup by hand.

    orchestrator-cli check                  # can I reach Proxmox and PBS?
    orchestrator-cli guests                 # what does Proxmox see?
    orchestrator-cli snapshots              # what's in the PBS datastore?
    orchestrator-cli services               # what's in my registry?
    orchestrator-cli backup example-media   # run one backup end to end

Every command honours ``DRY_RUN``; with it set (the default) nothing touches
real infrastructure and each intended action is logged instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

import click
from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterError, AdapterUnreachable
from orchestrator.adapters.factory import build_adapters
from orchestrator.config import get_settings
from orchestrator.db.models import Node, Service
from orchestrator.db.session import build_engine, get_engine
from orchestrator.logging_setup import configure_logging
from orchestrator.pipelines.backup import BackupError, BackupPipeline
from orchestrator.registry.loader import reconcile_yaml_into_db


def _bootstrap() -> None:
    configure_logging()
    build_engine()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


@click.group()
def cli() -> None:
    """homelab-orchestrator operator CLI."""


@cli.command()
def check() -> None:
    """Check connectivity to Proxmox and PBS."""
    _bootstrap()
    settings = get_settings()
    adapters = build_adapters(settings)

    async def _go() -> int:
        await adapters.start_all()
        failures = 0
        try:
            click.echo(f"dry_run           : {settings.dry_run}")
            click.echo(f"proxmox host      : {settings.proxmox_host}:{settings.proxmox_port}")
            click.echo(f"pbs host          : {settings.pbs_host}:{settings.pbs_port}")
            click.echo(f"pve backup storage: {settings.pve_backup_storage}")
            click.echo(f"pbs datastore     : {settings.pbs_datastore}")
            click.echo("")

            for label, coro in (
                ("Proxmox VE", adapters.proxmox.version()),
                ("PBS", adapters.pbs.version()),
            ):
                try:
                    info = await coro
                    click.secho(f"  OK    {label}: version {info.version}", fg="green")
                except AdapterUnreachable as exc:
                    click.secho(f"  FAIL  {label}: unreachable — {exc}", fg="red")
                    failures += 1
                except AdapterError as exc:
                    click.secho(f"  FAIL  {label}: {type(exc).__name__} — {exc}", fg="red")
                    failures += 1

            try:
                ds = await adapters.pbs.datastore_status(settings.pbs_datastore)
                click.secho(
                    f"  OK    datastore {ds.name!r}: "
                    f"{_fmt_bytes(ds.used_bytes)} used / {_fmt_bytes(ds.total_bytes)} "
                    f"({ds.used_fraction:.1%})",
                    fg="green",
                )
            except AdapterError as exc:
                click.secho(f"  FAIL  datastore {settings.pbs_datastore!r}: {exc}", fg="red")
                failures += 1
        finally:
            await adapters.stop_all()
        return failures

    failures = _run(_go())
    if failures:
        raise SystemExit(1)
    click.secho("\nAll checks passed.", fg="green", bold=True)


@cli.command()
@click.option("--node", default=None, help="Only show guests on this node.")
def guests(node: str | None) -> None:
    """List guests Proxmox can see."""
    _bootstrap()
    adapters = build_adapters()

    async def _go() -> None:
        await adapters.start_all()
        try:
            rows = await adapters.proxmox.list_guests(node)
            if not rows:
                click.echo("(no guests returned)")
                return
            click.echo(f"{'VMID':>6}  {'KIND':<4}  {'NODE':<16}  {'STATUS':<9}  NAME")
            for g in rows:
                click.echo(f"{g.vmid:>6}  {g.kind.value:<4}  {g.node:<16}  {g.status:<9}  {g.name}")
        finally:
            await adapters.stop_all()

    _run(_go())


@cli.command()
@click.option("--type", "backup_type", default=None, help="Filter: vm, ct, host.")
@click.option("--id", "backup_id", default=None, help="Filter by backup id, e.g. 9001.")
def snapshots(backup_type: str | None, backup_id: str | None) -> None:
    """List snapshots in the PBS datastore."""
    _bootstrap()
    settings = get_settings()
    adapters = build_adapters(settings)

    async def _go() -> None:
        await adapters.start_all()
        try:
            rows = await adapters.pbs.list_snapshots(
                settings.pbs_datastore, backup_type=backup_type, backup_id=backup_id
            )
            if not rows:
                click.echo("(no snapshots)")
                return
            click.echo(f"{'SNAPSHOT':<44}  {'SIZE':>10}  {'VERIFIED':<8}  PROTECTED")
            for s in rows:
                click.echo(
                    f"{s.snapshot_id:<44}  {_fmt_bytes(s.size_bytes):>10}  "
                    f"{s.verified!s:<8}  {s.protected}"
                )
        finally:
            await adapters.stop_all()

    _run(_go())


@cli.command()
def services() -> None:
    """List services in the registry (reconciles from YAML first)."""
    _bootstrap()
    settings = get_settings()
    with Session(get_engine()) as session:
        if settings.services_yaml_path.exists():
            reconcile_yaml_into_db(session, settings.services_yaml_path)
            session.commit()
        rows = session.exec(select(Service).order_by(Service.slug)).all()
        nodes = {n.id: n.name for n in session.exec(select(Node)).all()}
        if not rows:
            click.echo("(no services registered)")
            return
        click.echo(f"{'SLUG':<20}  {'NODE':<18}  {'GUEST':<10}  ENABLED")
        for s in rows:
            guest = f"{s.guest_kind.value}/{s.guest_id}" if s.guest_id else s.guest_kind.value
            click.echo(f"{s.slug:<20}  {nodes.get(s.node_id, '-'):<18}  {guest:<10}  {s.enabled}")


@cli.command()
@click.argument("slug")
@click.option("--no-verify", is_flag=True, help="Skip the PBS verify step.")
def backup(slug: str, no_verify: bool) -> None:
    """Run a backup for SLUG end to end."""
    _bootstrap()
    settings = get_settings()
    adapters = build_adapters(settings)

    if settings.dry_run:
        click.secho("DRY_RUN is on — no real backup will be taken.", fg="yellow")
    click.secho(
        "NOTE: the phase-4 integrity gate is not implemented yet — this backup " "runs UNGATED.",
        fg="yellow",
    )

    async def _go() -> None:
        await adapters.start_all()
        try:
            pipeline = BackupPipeline(proxmox=adapters.proxmox, pbs=adapters.pbs, settings=settings)
            with Session(get_engine()) as session:
                if settings.services_yaml_path.exists():
                    reconcile_yaml_into_db(session, settings.services_yaml_path)
                    session.commit()
                run = await pipeline.run_for_service(
                    session, slug, actor="cli", verify=not no_verify
                )
                click.echo("")
                click.secho(f"Run #{run.id} — {run.status.value}", fg="green", bold=True)
                for step in run.steps:
                    click.echo(f"  {step['status']:<13} {step['name']}")
        finally:
            await adapters.stop_all()

    try:
        _run(_go())
    except (BackupError, AdapterError) as exc:
        click.secho(f"\nBackup failed: {exc}", fg="red", bold=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    cli()
