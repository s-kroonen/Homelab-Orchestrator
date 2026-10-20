"""``orchestrator-cli ip`` — show and discover service IP addresses.

``ip show`` lists every service's effective IP (manual ``ip_address`` first,
else what discovery found), its source and the guest's NIC MACs.
``ip discover`` runs discovery now — the same :class:`IpDiscoverer` the
periodic job and the dashboard's "Discover now" button use: guest agent / LXC
first, then ARP, then DHCP; stopped guests are skipped. Results are runtime
state in the database, never written to services.yaml. To set a manual IP use
``orchestrator-cli service update SLUG --ip 10.0.0.5``. See docs/service_ips.md.
"""

from __future__ import annotations

import asyncio

import click
from sqlmodel import Session, select

from orchestrator.adapters.factory import build_adapters
from orchestrator.config import get_settings
from orchestrator.db.models import Service, ServiceState
from orchestrator.db.session import build_engine, get_engine
from orchestrator.ipdiscovery.discovery import SOURCE_LABELS, IpDiscoverer, ip_view
from orchestrator.logging_setup import configure_logging
from orchestrator.registry.loader import reconcile_yaml_into_db


def _session_with_registry() -> Session:
    configure_logging()
    build_engine()
    settings = get_settings()
    session = Session(get_engine())
    if settings.services_yaml_path.exists():
        reconcile_yaml_into_db(session, settings.services_yaml_path)
        session.commit()
    return session


@click.group("ip")
def ip_group() -> None:
    """Service IP addresses: show them, discover them."""


@ip_group.command("show")
@click.argument("slugs", nargs=-1)
def ip_show(slugs: tuple[str, ...]) -> None:
    """Effective IP, source and MACs of every service (or just SLUGS)."""
    with _session_with_registry() as session:
        stmt = select(Service).order_by(Service.slug)
        if slugs:
            stmt = stmt.where(Service.slug.in_(slugs))  # type: ignore[attr-defined]
        services = session.exec(stmt).all()
        if not services:
            raise click.ClickException("no service matched")
        click.echo(f"{'SLUG':<24} {'IP':<40} {'SOURCE':<12} MACS")
        for svc in services:
            view = ip_view(svc, session.get(ServiceState, svc.id))
            source = SOURCE_LABELS.get(view["effective_source"], view["effective_source"])
            macs = ", ".join(view["mac_addresses"]) or "-"
            click.echo(
                f"{svc.slug:<24} {view['effective_ip'] or '-':<40} {source or '-':<12} {macs}"
            )
            if view["manual_ip"] and view["discovered_ip"] not in ("", view["manual_ip"]):
                click.secho(
                    f"{'':<24} discovered {view['discovered_ip']} "
                    f"({view['ip_source_label']}) — the manual IP wins",
                    fg="yellow",
                )
            if view["ip_discovery_error"] and not view["effective_ip"]:
                click.secho(f"{'':<24} {view['ip_discovery_error']}", fg="yellow")


@ip_group.command("discover")
@click.argument("slugs", nargs=-1)
@click.option("--all", "discover_all", is_flag=True, help="Every enabled service with a guest.")
def ip_discover(slugs: tuple[str, ...], discover_all: bool) -> None:
    """Discover the IP of SLUG... (or --all) now and store it.

    Exits 1 if any named service ended in an error.
    """
    if not slugs and not discover_all:
        raise click.UsageError("give one or more SLUGs or --all")
    if slugs and discover_all:
        raise click.UsageError("SLUGs and --all are mutually exclusive")
    settings = get_settings()
    session = _session_with_registry()
    adapters = build_adapters(settings)

    async def _go() -> int:
        await adapters.proxmox.start()
        try:
            discoverer = IpDiscoverer(settings=settings, proxmox=adapters.proxmox)
            cycle = discoverer.new_cycle()
            stmt = select(Service).order_by(Service.slug)
            if slugs:
                stmt = stmt.where(Service.slug.in_(slugs))  # type: ignore[attr-defined]
            else:
                stmt = stmt.where(Service.enabled == True)  # noqa: E712
            services = session.exec(stmt).all()
            missing = sorted(set(slugs) - {s.slug for s in services})
            for slug in missing:
                click.secho(f"{slug:<24} not in the registry", fg="red")
            errors = len(missing)
            colour = {"found": "green", "not_found": "yellow", "skipped": None, "error": "red"}
            for svc in services:
                if discover_all and (svc.guest_kind.value == "none" or svc.guest_id is None):
                    continue
                r = await discoverer.discover(session, svc, cycle=cycle, actor="cli")
                errors += r.status == "error"
                label = {"found": "FOUND", "not_found": "none", "skipped": "skip", "error": "ERROR"}
                click.secho(f"{svc.slug:<24} {label[r.status]:<6} {r.message}", fg=colour[r.status])
                if r.effective_ip:
                    note = " (changed)" if r.changed else ""
                    src = SOURCE_LABELS.get(r.effective_source, r.effective_source)
                    click.echo(f"{'':<24} effective IP {r.effective_ip} [{src}]{note}")
            return errors
        finally:
            await adapters.proxmox.stop()
            session.close()

    if asyncio.run(_go()):
        raise SystemExit(1)


__all__ = ["ip_group"]
