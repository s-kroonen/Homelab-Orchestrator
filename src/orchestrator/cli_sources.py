"""``orchestrator-cli sources`` - pull what lives in other systems, from the shell.

The same actions as the Data sources page: ``pull proxmox`` (fill node addresses, check nodes,
re-read guests), ``pull ips`` (discover addresses), ``pull traefik`` (import routes). Routes are
formatted with ``orchestrator-cli proxy format``. See docs/sources.md.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import click
from sqlmodel import Session, select

from orchestrator.adapters.factory import build_adapters
from orchestrator.config import get_settings
from orchestrator.db.models import Service
from orchestrator.db.session import build_engine, get_engine
from orchestrator.logging_setup import configure_logging


@click.group()
def sources() -> None:
    """Pull data that belongs to Proxmox, Traefik Manager and the guests."""


def _run(go: Any) -> Any:
    configure_logging()
    build_engine()
    settings = get_settings()
    adapters = build_adapters(settings)
    if settings.dry_run:
        click.secho("DRY_RUN is on - the dry-run adapters are used.", fg="yellow")

    async def main() -> Any:
        await adapters.start_all()
        try:
            with Session(get_engine()) as session:
                return await go(session, adapters, settings)
        finally:
            await adapters.stop_all()

    return asyncio.run(main())


class _Admin:
    username = "cli"


@sources.command("pull")
@click.argument("what", type=click.Choice(["proxmox", "ips", "traefik"]))
@click.option("--only-missing", is_flag=True, help="ips: only services without an address.")
@click.option("--json", "as_json", is_flag=True)
def pull(what: str, only_missing: bool, as_json: bool) -> None:
    """Refresh WHAT from where it lives."""

    async def go(session: Session, adapters: Any, settings: Any) -> Any:
        from orchestrator.web.routers import infra, proxy_hosts
        from orchestrator.web.routers.dashboard import discover_service_ip

        admin: Any = _Admin()
        if what == "proxmox":
            filled = await infra.fill_node_addresses(session, adapters, settings, admin)
            resync = await infra.do_resync(session, adapters, settings, "cli")
            return {"addresses": filled, "guests": resync}
        if what == "ips":
            found = []
            for svc in session.exec(select(Service).order_by(Service.slug)).all():
                if svc.guest_id is None or (only_missing and svc.ip_address):
                    continue
                got = await discover_service_ip(session, adapters, settings, svc.slug, actor="cli")
                found.append({"slug": svc.slug, "ip": got.get("effective_ip") or ""})
            return {"scanned": len(found), "results": found}
        return await proxy_hosts.resync_proxy_hosts(session, admin, settings, adapters)

    out = _run(go)
    if as_json:
        click.echo(json.dumps(out, indent=2, default=str))
        return
    if what == "proxmox":
        filled = out["addresses"]["filled"]
        click.echo("filled: " + (", ".join(f"{n}={a}" for n, a in filled.items()) or "nothing"))
        g = out["guests"]
        click.echo(
            f"re-read {g['scanned']} service(s); not found in Proxmox: {g['missing'] or 'none'}"
        )
        for u in g["unregistered_guests"]:
            click.echo(f"  not registered: {u['node']}/{u['vmid']} {u['name']}")
    elif what == "ips":
        for r in out["results"]:
            click.echo(f"{r['slug']:<24} {r['ip'] or '-'}")
    else:
        click.echo(f"updated {out['updated_count']}, discovered {out['discovered_count']}")
