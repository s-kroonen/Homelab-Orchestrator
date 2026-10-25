"""``orchestrator-cli service state|enable|disable`` - the dashboard's linked-state panel and
its one switch, from the shell.

Same functions as the page and ``/api/services/{slug}/linked|switch``: what is really on is
read from Traefik Manager, Portainer and Proxmox; ``enable`` / ``disable`` act on all of it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import click
from sqlmodel import Session, select

from orchestrator.adapters.factory import build_adapters
from orchestrator.cli_config import open_editor, service
from orchestrator.config import get_settings
from orchestrator.db.models import Service
from orchestrator.db.session import build_engine, get_engine
from orchestrator.logging_setup import configure_logging
from orchestrator.registry.editor import RegistryEditError
from orchestrator.registry.linked import LinkedState, read_linked
from orchestrator.registry.switch import SwitchRefused, SwitchResult, switch_service

_COLOUR = {"on": "green", "off": "red", "mixed": "yellow", "unknown": "yellow", "none": None}
_MARK = {True: "on ", False: "off", None: "?  "}


def _bootstrap() -> None:
    configure_logging()
    build_engine()


def _print_state(state: LinkedState) -> None:
    head = f"{state.slug}: "
    click.echo(head, nl=False)
    click.secho(state.state.upper(), fg=_COLOUR.get(state.state), bold=True, nl=False)
    click.echo(f"   (asked to be {'on' if state.served else 'off'})")
    if not state.parts:
        click.echo("  nothing is linked (no proxy host, controller or guest)")
        return
    for p in state.parts:
        extra = " (own switch)" if p.deliberate else ""
        click.echo(f"  {_MARK[p.on]}  {p.label:<44} {p.detail}{extra}")
    if state.drift:
        click.secho(f"  ! {state.drift}", fg="yellow")


def _print_result(result: SwitchResult) -> None:
    for step in result.steps:
        colour = {"ok": "green", "skipped": None, "failed": "red"}.get(step["status"])
        detail = step["detail"]
        note = detail.get("error") or detail.get("message") or detail.get("reason") or ""
        click.echo(f"  {step['status']:<8}", nl=False)
        click.secho(f" {step['name']}", fg=colour, nl=False)
        click.echo(f"  {note}" if note else "")
    if result.state is not None:
        _print_state(result.state)


def _mirror_intent_to_yaml(slug: str, enabled: bool) -> None:
    """The CLI's registry is the YAML file: keep what was asked for in it, so the next
    reconcile does not put the old value back."""
    try:
        editor = open_editor()
        if editor.get_service(slug) is None:
            return
        editor.upsert_service(slug, {"enabled": enabled})
        editor.save()
    except (RegistryEditError, click.ClickException) as exc:
        click.secho(f"The registry file was not updated: {exc}", fg="yellow")


def _find(session: Session, slug: str) -> Service:
    svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
    if svc is None:
        raise click.ClickException(f"no service {slug!r}")
    return svc


@service.command("state")
@click.argument("slug")
@click.option("--json", "as_json", is_flag=True)
def service_state(slug: str, as_json: bool) -> None:
    """Is SLUG really on? Reads its proxy hosts, stack / container and guest."""
    _bootstrap()
    settings = get_settings()
    adapters = build_adapters(settings)

    async def go() -> LinkedState:
        await adapters.start_all()
        try:
            with Session(get_engine()) as session:
                return await read_linked(session, adapters, settings, _find(session, slug))
        finally:
            await adapters.stop_all()

    state = asyncio.run(go())
    if as_json:
        click.echo(json.dumps(state.as_dict(), indent=2))
        return
    _print_state(state)


def _switch(slug: str, want: bool, include_guest: bool, restore_children: bool) -> None:
    _bootstrap()
    settings = get_settings()
    adapters = build_adapters(settings)
    if settings.dry_run:
        click.secho("DRY_RUN is on - nothing real is changed.", fg="yellow")

    async def go() -> SwitchResult:
        await adapters.start_all()
        try:
            with Session(get_engine()) as session:
                return await switch_service(
                    session,
                    adapters,
                    settings,
                    slug,
                    want,
                    actor="cli",
                    include_guest=include_guest,
                    restore_children=restore_children,
                )
        finally:
            await adapters.stop_all()

    try:
        result: Any = asyncio.run(go())
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    except SwitchRefused as exc:
        raise click.ClickException(str(exc)) from exc
    _print_result(result)
    _mirror_intent_to_yaml(slug, want)
    if not result.ok:
        raise SystemExit(1)


@service.command("disable")
@click.argument("slug")
@click.option(
    "--stop-guest",
    "include_guest",
    is_flag=True,
    help="Also stop the Proxmox guest (a root service). Without it the guest is left running.",
)
def service_disable(slug: str, include_guest: bool) -> None:
    """Switch SLUG off: unserve its proxy hosts, stop its stack or container, then (with
    --stop-guest) the guest - everything below it first."""
    _switch(slug, False, include_guest, False)


@service.command("enable")
@click.argument("slug")
@click.option(
    "--restore-children",
    is_flag=True,
    help="Also start the sub-services that were on, instead of only watching them.",
)
def service_enable(slug: str, restore_children: bool) -> None:
    """Switch SLUG on: guest, then stack or container (waited for), then serve its proxy hosts."""
    _switch(slug, True, False, restore_children)
