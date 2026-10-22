"""``orchestrator-cli notify`` - test the channels and read the feed."""

from __future__ import annotations

import asyncio

import click
from sqlmodel import Session, col, select

from orchestrator.adapters.factory import build_notifier
from orchestrator.config import get_settings
from orchestrator.db.models import NotificationEvent, NotificationRule
from orchestrator.db.session import build_engine, get_engine
from orchestrator.notify.engine import Event, NotificationEngine


def _open() -> tuple[Session, NotificationEngine]:
    settings = get_settings()
    build_engine(settings)
    return Session(get_engine()), NotificationEngine(build_notifier(settings), settings)


@click.group()
def notify() -> None:
    """Notification channels, rules and the event feed."""


@notify.command("test")
@click.option("--channel", "channels", multiple=True, help="Only this channel (repeatable).")
def notify_test(channels: tuple[str, ...]) -> None:
    """Send a test notification and show what each channel did."""
    settings = get_settings()
    session, engine = _open()
    with session:
        if settings.dry_run:
            click.secho("DRY_RUN=true: the notifier only logs.", fg="yellow")
        row = asyncio.run(
            engine.emit_direct(
                session,
                Event(
                    "service",
                    "test",
                    "test",
                    "test",
                    "info",
                    "Test notification",
                    "Sent from orchestrator-cli. If you can read this, this channel works.",
                ),
                channels=list(channels) or None,
            )
        )
    if not row.delivered:
        click.secho(row.suppressed_reason or "No channel is active.", fg="red")
        raise SystemExit(1)
    bad = 0
    for name, result in row.delivered.items():
        ok = result == "ok"
        bad += not ok
        click.secho(f"  {'OK  ' if ok else 'FAIL'}  {name}: {result}", fg="green" if ok else "red")
    if bad:
        raise SystemExit(1)


@notify.command("feed")
@click.option("--limit", default=30, show_default=True)
def notify_feed(limit: int) -> None:
    """The latest events the engine decided on."""
    _open()
    with Session(get_engine()) as session:
        rows = session.exec(
            select(NotificationEvent).order_by(col(NotificationEvent.id).desc()).limit(limit)
        ).all()
    for e in rows:
        note = f"  [{e.suppressed_reason}]" if e.suppressed else ""
        click.echo(
            f"{e.at:%Y-%m-%d %H:%M}  {e.level:<8} {e.target:<24} {e.event:<18} {e.title}{note}"
        )
    if not rows:
        click.echo("Nothing yet.")


@notify.command("rules")
def notify_rules() -> None:
    """List the rules (none = the defaults apply)."""
    _open()
    with Session(get_engine()) as session:
        rules = session.exec(select(NotificationRule).order_by(NotificationRule.id)).all()
    if not rules:
        click.echo("No rules - failed / down / recovered go to every channel.")
    for r in rules:
        click.echo(
            f"{r.name:<28} {'on ' if r.enabled else 'off'} levels={r.levels or 'any'} "
            f"events={r.events or 'any'} from={r.min_severity} channels={r.channels or 'all'}"
        )
