"""``orchestrator-cli portainer`` - read what Portainer has, from the shell.

The same calls as the dashboard's Portainer page (and `/api/portainer`): environments,
stacks, containers, logs, stats. Read-only; Portainer stays the owner of stacks.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime
from typing import Any

import click

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.factory import build_adapters
from orchestrator.adapters.portainer.base import PortainerAdapter
from orchestrator.adapters.portainer.parse import epoch_from_iso
from orchestrator.config import get_settings
from orchestrator.db.session import build_engine
from orchestrator.logging_setup import configure_logging


def _with_adapter(fn: Callable[[PortainerAdapter], Awaitable[Any]]) -> Any:
    configure_logging()
    build_engine()
    named = click.get_current_context().meta.get("portainer_instance")
    adapter = build_adapters(get_settings()).portainer_for(named)
    if adapter is None:
        raise click.ClickException(f"there is no Portainer connection called {named!r}")

    async def go() -> Any:
        await adapter.start()
        try:
            return await fn(adapter)
        finally:
            await adapter.stop()

    try:
        return asyncio.run(go())
    except AdapterError as exc:
        raise click.ClickException(str(exc)) from exc


def _short(text: str | None, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def _when(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


@click.group()
@click.option(
    "--instance",
    "-i",
    default=None,
    help="Which Portainer connection (its id on the Connections page); default: the default one.",
)
@click.pass_context
def portainer(ctx: click.Context, instance: str | None) -> None:
    """What Portainer has: environments, stacks, containers, logs (read-only)."""
    ctx.meta["portainer_instance"] = instance


@portainer.command("status")
@click.option("--json", "as_json", is_flag=True)
def portainer_status(as_json: bool) -> None:
    """Is Portainer reachable, and which version?"""
    check = _with_adapter(lambda a: a.check())
    if as_json:
        click.echo(json.dumps(asdict(check), indent=2))
        return
    color = "green" if check.ok else ("yellow" if not check.configured else "red")
    click.secho(check.detail, fg=color)
    if not check.ok:
        raise SystemExit(0 if not check.configured else 1)


@portainer.command("environments")
@click.option("--json", "as_json", is_flag=True)
def portainer_environments(as_json: bool) -> None:
    """The environments (Docker hosts) Portainer manages."""
    rows = _with_adapter(lambda a: a.list_environments())
    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2))
        return
    click.echo(f"{'ID':<4} {'NAME':<22} {'TYPE':<8} {'STATUS':<7} {'RUNNING':<8} {'STACKS':<7} URL")
    for e in rows:
        click.echo(
            f"{e.id:<4} {_short(e.name, 22):<22} {e.type:<8} {e.status:<7} "
            f"{e.running if e.running is not None else '-':<8} "
            f"{e.stacks if e.stacks is not None else '-':<7} {e.url}"
        )


@portainer.command("stacks")
@click.option("--endpoint", "-e", type=int, default=None, help="Only this environment id.")
@click.option("--json", "as_json", is_flag=True)
def portainer_stacks(endpoint: int | None, as_json: bool) -> None:
    """Stacks, including compose projects Portainer did not create."""

    async def go(a: PortainerAdapter) -> Any:
        listing = await a.list_stacks(endpoint)
        kinds = await asyncio.gather(*(a.stack_kind(s) for s in listing.stacks))
        return listing, kinds

    listing, kinds = _with_adapter(go)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "stacks": [
                        {**asdict(s), "kind": k} for s, k in zip(listing.stacks, kinds, strict=True)
                    ],
                    "errors": {str(k): v for k, v in listing.errors.items()},
                },
                indent=2,
            )
        )
        return
    click.echo(
        f"{'ENV':<4} {'NAME':<24} {'STATUS':<9} {'KIND':<8} {'MANAGED':<8} {'GIT REF':<24} UPDATED"
    )
    for s, k in zip(listing.stacks, kinds, strict=True):
        ref = (s.git.reference.removeprefix("refs/heads/") + "@" + s.git.hash[:7]) if s.git else "-"
        click.echo(
            f"{s.endpoint_id:<4} {_short(s.name, 24):<24} {s.status:<9} {k:<8} "
            f"{'yes' if s.managed else 'no':<8} {_short(ref, 24):<24} {_when(s.updated_at)}"
        )
    for env_id, why in listing.errors.items():
        click.secho(f"  environment {env_id}: {why}", fg="yellow")


@portainer.command("containers")
@click.option("--endpoint", "-e", type=int, required=True, help="Environment id.")
@click.option("--stack", "-s", default=None, help="Only this stack's containers.")
@click.option("--running", is_flag=True, help="Hide stopped containers.")
@click.option("--json", "as_json", is_flag=True)
def portainer_containers(endpoint: int, stack: str | None, running: bool, as_json: bool) -> None:
    """Containers of an environment (or one stack)."""
    rows = _with_adapter(
        lambda a: a.list_containers(endpoint, stack=stack, include_stopped=not running)
    )
    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2))
        return
    click.echo(f"{'NAME':<34} {'STATE':<10} {'HEALTH':<10} {'STACK':<18} IMAGE")
    for c in rows:
        click.echo(
            f"{_short(c.name, 34):<34} {c.state:<10} {c.health or '-':<10} "
            f"{_short(c.stack, 18) if c.stack else '-':<18} {_short(c.image, 40)}"
        )


@portainer.command("logs")
@click.argument("container")
@click.option("--endpoint", "-e", type=int, required=True, help="Environment id.")
@click.option("--tail", "-n", type=int, default=100, show_default=True)
@click.option("--follow", "-f", is_flag=True, help="Keep printing new lines (Ctrl-C to stop).")
@click.option("--no-timestamps", is_flag=True)
def portainer_logs(
    container: str, endpoint: int, tail: int, follow: bool, no_timestamps: bool
) -> None:
    """A container's logs (name or id)."""

    async def go(a: PortainerAdapter) -> None:
        seen: set[tuple[str | None, str]] = set()
        lines = await a.logs(endpoint, container, tail=tail, timestamps=not no_timestamps)
        last = 0
        while True:
            for line in lines:
                key = (line.ts, line.text)
                if key in seen:
                    continue
                seen.add(key)
                if len(seen) > 5000:
                    seen.clear()
                prefix = f"{line.ts} " if line.ts else ""
                click.secho(prefix + line.text, fg="red" if line.stream == "stderr" else None)
                last = epoch_from_iso(line.ts) or last
            if not follow:
                return
            await asyncio.sleep(2)
            lines = await a.logs(
                endpoint, container, tail=1000, since=max(0, last - 1), timestamps=True
            )

    try:
        _with_adapter(go)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        click.echo("")


@portainer.command("stats")
@click.argument("container")
@click.option("--endpoint", "-e", type=int, required=True, help="Environment id.")
def portainer_stats(container: str, endpoint: int) -> None:
    """One reading of a container's CPU, memory and network."""
    s = _with_adapter(lambda a: a.stats(endpoint, container))
    click.echo(
        f"cpu {s.cpu_percent:.1f}%  memory {s.mem_bytes / 1e6:.0f} MB of {s.mem_limit / 1e6:.0f} MB  "
        f"net in {s.net_rx_bytes / 1e6:.1f} MB out {s.net_tx_bytes / 1e6:.1f} MB  pids {s.pids}"
    )


__all__ = ["portainer"]
