"""``orchestrator-cli bulk`` - one action on many services or proxy hosts.

The command-line twin of the dashboard's selection checkboxes + action bar
(``POST /api/bulk/*``). Pick the items with ``--all``, ``--group`` (services)
/ ``--service`` (proxy hosts), or an explicit comma list; every item is
reported on its own line as ``ok`` or ``FAILED: <why>``, and the command exits
1 if any item failed. ``--dry-run`` only prints what would be done.

What each action touches follows the existing single-item commands exactly:

* ``bulk services enable|disable|probes-required|probes-optional`` edit
  ``services.yaml`` (like ``service update --enabled`` / ``probe add
  --required``), one save for the whole batch.
* ``bulk services wake`` runs the wake pipeline per service, one after
  another (like ``wake SLUG``). Honours ``DRY_RUN``.
* ``bulk proxy-hosts enable|disable`` toggle the live route on Traefik
  Manager first (the same function the web uses), then store the flag in the
  database and, when the host is in ``services.yaml``, there too - so a later
  YAML reload cannot quietly undo it. Honours ``DRY_RUN`` (dry-run proxy
  adapter: nothing is sent to Traefik Manager). Audited with actor ``cli``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import click
from sqlmodel import Session, col, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.factory import build_adapters
from orchestrator.audit import log as audit
from orchestrator.cli_config import _split_slugs, open_editor, save
from orchestrator.config import get_settings
from orchestrator.db.models import ProxyHost, Service
from orchestrator.db.session import build_engine, get_engine
from orchestrator.logging_setup import configure_logging

SERVICE_ACTIONS = ("enable", "disable", "wake", "probes-required", "probes-optional")
PROXY_ACTIONS = ("enable", "disable")


def _report(results: list[tuple[str, bool, str]], what: str) -> None:
    ok = sum(1 for _, good, _ in results if good)
    for item, good, message in results:
        if good:
            click.echo(f"  ok      {item:<32} {message}")
        else:
            click.secho(f"  FAILED  {item:<32} {message}", fg="red")
    colour = "green" if ok == len(results) else "yellow" if ok else "red"
    click.secho(f"\n{what}: {ok} ok, {len(results) - ok} failed.", fg=colour, bold=True)
    if ok != len(results):
        raise SystemExit(1)


def _pick(available: list[str], *, pick_all: bool, explicit: str | None, label: str) -> list[str]:
    if pick_all and explicit:
        raise click.UsageError(f"--all and --{label} are mutually exclusive")
    if pick_all:
        return list(available)
    if explicit:
        return _split_slugs(explicit)
    return []


@click.group()
def bulk() -> None:
    """One action on many services or proxy hosts at once."""


@bulk.command("services")
@click.argument("action", type=click.Choice(SERVICE_ACTIONS))
@click.option("--all", "pick_all", is_flag=True, help="Every service in services.yaml.")
@click.option("--group", default=None, help="Every service in this dashboard group.")
@click.option("--slug", "slugs", default=None, help="Comma-separated slugs, e.g. media,photos.")
@click.option("--dry-run", is_flag=True, help="Only print what would be done.")
@click.option("--file", "path_override", default=None)
def bulk_services(
    action: str,
    pick_all: bool,
    group: str | None,
    slugs: str | None,
    dry_run: bool,
    path_override: str | None,
) -> None:
    """Run ACTION on the selected services.

    enable/disable/probes-required/probes-optional edit services.yaml;
    wake runs the wake pipeline for each, one at a time.
    """
    editor = open_editor(path_override)
    known = editor.service_slugs()
    chosen = _pick(known, pick_all=pick_all, explicit=slugs, label="slug")
    if group is not None:
        in_group = [
            str(s.get("slug"))
            for s in editor.doc.get("services") or []
            if str(s.get("group") or "") == group
        ]
        chosen = [s for s in chosen if s in in_group] if (pick_all or slugs) else in_group
    if not chosen:
        raise click.UsageError("nothing selected - pass --all, --group or --slug")

    if dry_run:
        click.secho(f"Would {action}: {', '.join(chosen)}", fg="cyan")
        return

    results: list[tuple[str, bool, str]] = []
    if action == "wake":
        _bulk_wake(chosen, results)
        _report(results, f"wake {len(chosen)} service(s)")
        return

    for slug in chosen:
        svc = editor.get_service(slug)
        if svc is None:
            results.append((slug, False, "not in the registry"))
            continue
        if action in ("enable", "disable"):
            editor.upsert_service(slug, {"enabled": action == "enable"})
            results.append((slug, True, f"{action}d"))
            continue
        required = action == "probes-required"
        probes = svc.get("probes") or []
        changed = 0
        for probe in probes:
            if bool(probe.get("required", True)) != required:
                probe["required"] = required
                changed += 1
        word = "required" if required else "optional"
        results.append(
            (
                slug,
                True,
                f"{changed} of {len(probes)} probe(s) now {word}" if probes else "no probes",
            )
        )
    if any(good for _, good, _ in results):
        save(editor, f"bulk {action} on {len(chosen)} service(s)")
    _report(results, f"{action} {len(chosen)} service(s)")


def _bulk_wake(slugs: list[str], results: list[tuple[str, bool, str]]) -> None:
    from orchestrator.pipelines.wake import WakeError, WakePipeline
    from orchestrator.registry.loader import reconcile_yaml_into_db

    configure_logging()
    build_engine()
    settings = get_settings()
    if settings.dry_run:
        click.secho("DRY_RUN is on — no real power/proxmox action will be taken.", fg="yellow")
    adapters = build_adapters(settings)

    async def _go() -> None:
        await adapters.start_all()
        try:
            pipeline = WakePipeline(
                power=adapters.power, proxmox=adapters.proxmox, settings=settings
            )
            with Session(get_engine()) as session:
                if settings.services_yaml_path.exists():
                    reconcile_yaml_into_db(session, settings.services_yaml_path)
                    session.commit()
                for slug in slugs:
                    click.echo(f"waking {slug}…")
                    try:
                        run = await pipeline.run_for_service(session, slug, actor="cli")
                    except (WakeError, AdapterError) as exc:
                        session.rollback()
                        results.append((slug, False, str(exc)))
                        continue
                    good = run.status.value == "succeeded"
                    results.append(
                        (slug, good, f"run #{run.id} {run.status.value} {run.error or ''}".strip())
                    )
        finally:
            await adapters.stop_all()

    asyncio.run(_go())


@bulk.command("proxy-hosts")
@click.argument("action", type=click.Choice(PROXY_ACTIONS))
@click.option("--all", "pick_all", is_flag=True, help="Every proxy host.")
@click.option("--service", "service_slug", default=None, help="Every host of this service.")
@click.option("--hostname", "hostnames", default=None, help="Comma-separated hostnames.")
@click.option("--dry-run", is_flag=True, help="Only print what would be done.")
@click.option("--file", "path_override", default=None)
def bulk_proxy_hosts(
    action: str,
    pick_all: bool,
    service_slug: str | None,
    hostnames: str | None,
    dry_run: bool,
    path_override: str | None,
) -> None:
    """Enable or disable the selected proxy hosts.

    A live-managed (traefik) host is toggled on Traefik Manager first; a
    failure there leaves that host untouched and is reported. The flag is then
    stored in the database and in services.yaml.
    """
    from orchestrator.web.routers.proxy_hosts import set_proxy_host_enabled

    configure_logging()
    build_engine()
    settings = get_settings()
    enabled = action == "enable"

    with Session(get_engine()) as session:
        query = select(ProxyHost).order_by(col(ProxyHost.hostname))
        rows = list(session.exec(query).all())
        if service_slug is not None:
            svc = session.exec(select(Service).where(Service.slug == service_slug)).first()
            if svc is None:
                raise click.ClickException(f"no service registered with slug {service_slug!r}")
            rows = [r for r in rows if r.service_id == svc.id]
            if not (pick_all or hostnames):
                pick_all = True
        wanted = _pick(
            [r.hostname for r in rows], pick_all=pick_all, explicit=hostnames, label="hostname"
        )
        if not wanted:
            raise click.UsageError("nothing selected - pass --all, --service or --hostname")
        by_name = {r.hostname: r for r in rows}

        if dry_run:
            click.secho(f"Would {action}: {', '.join(wanted)}", fg="cyan")
            return
        if settings.dry_run:
            click.secho(
                "DRY_RUN is on — Traefik Manager is not contacted; only the records change.",
                fg="yellow",
            )

        actor = SimpleNamespace(username="cli")
        results: list[tuple[str, bool, str]] = []
        done: list[ProxyHost] = []

        async def _go() -> None:
            adapters = build_adapters(settings)
            await adapters.proxy.start()
            try:
                for hostname in wanted:
                    ph = by_name.get(hostname.lower())
                    if ph is None:
                        results.append((hostname, False, "no such proxy host"))
                        continue
                    try:
                        await set_proxy_host_enabled(
                            session, actor, settings, adapters, ph, enabled  # type: ignore[arg-type]
                        )
                    except Exception as exc:
                        session.rollback()
                        detail: Any = getattr(exc, "detail", None) or str(exc)
                        results.append((hostname, False, str(detail)))
                        continue
                    done.append(ph)
                    results.append((hostname, True, f"{action}d"))
            finally:
                await adapters.proxy.stop()

        asyncio.run(_go())
        audit.record(
            session,
            actor="cli",
            action="bulk.proxy_hosts",
            target="bulk:proxy_hosts",
            details={
                "action": action,
                "params": {},
                "count": len(results),
                "ok": [h for h, good, _ in results if good],
                "failed": [{"id": h, "message": m} for h, good, m in results if not good],
            },
        )
        session.commit()
        services = {s.id: s.slug for s in session.exec(select(Service)).all()}
        yaml_updates = [(services.get(ph.service_id), ph.hostname) for ph in done]

    _mirror_into_yaml(path_override, yaml_updates, enabled)
    _report(results, f"{action} {len(wanted)} proxy host(s)")


def _mirror_into_yaml(
    path_override: str | None, hosts: list[tuple[str | None, str]], enabled: bool
) -> None:
    """Write the new flag into services.yaml for hosts listed there (omitted
    when true - the default), so a YAML reload agrees with the gateway."""
    if not hosts:
        return
    editor = open_editor(path_override)
    touched = 0
    for slug, hostname in hosts:
        svc = editor.get_service(slug) if slug else None
        entries = svc.get("proxy_hosts") if svc is not None else None
        for entry in entries or []:
            if str(entry.get("hostname", "")).lower() == hostname:
                if enabled:
                    entry.pop("enabled", None)
                else:
                    entry["enabled"] = False
                touched += 1
    if touched:
        save(editor, f"{touched} proxy host(s) marked {'enabled' if enabled else 'disabled'}")
