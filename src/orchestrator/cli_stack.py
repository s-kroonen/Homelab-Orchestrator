"""``orchestrator-cli stack`` and ``service link-host|unlink-host`` - adopt, create, delete and
control Portainer stacks, and link proxy hosts, from the shell.

The same functions as the dashboard's Portainer page and ``/api/stacks``. Portainer owns the
stacks: create and delete go through its API; the orchestrator only keeps the link.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import click
from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterError
from orchestrator.adapters.factory import AdapterBundle, build_adapters
from orchestrator.adapters.portainer.base import StackCreate
from orchestrator.cli_config import service
from orchestrator.config import Settings, get_settings
from orchestrator.db.models import ProxyHost
from orchestrator.db.models import Service as ServiceRow
from orchestrator.db.session import build_engine, get_engine
from orchestrator.logging_setup import configure_logging
from orchestrator.registry import adopt as adoption
from orchestrator.registry import stacks as stack_ops
from orchestrator.registry.adopt import AdoptPlan, ChildPlan, StackError


def _run(fn: Callable[[Session, AdapterBundle, Settings], Awaitable[Any]]) -> Any:
    configure_logging()
    build_engine()
    settings = get_settings()
    adapters = build_adapters(settings)
    if settings.dry_run:
        click.secho("DRY_RUN is on - nothing real is changed.", fg="yellow")

    async def go() -> Any:
        await adapters.start_all()
        try:
            with Session(get_engine()) as session:
                return await fn(session, adapters, settings)
        finally:
            await adapters.stop_all()

    try:
        return asyncio.run(go())
    except StackError as exc:
        raise click.ClickException(str(exc)) from exc
    except AdapterError as exc:
        raise click.ClickException(str(exc)) from exc


def _instance() -> str | None:
    """The Portainer connection --instance named (None = the default one)."""
    return click.get_current_context().meta.get("portainer_instance")


@click.group()
@click.option(
    "--instance",
    "-i",
    default=None,
    help="Which Portainer connection (its id on the Connections page); default: the default one.",
)
@click.pass_context
def stack(ctx: click.Context, instance: str | None) -> None:
    """Adopt, create, delete and control Portainer stacks."""
    ctx.meta["portainer_instance"] = instance


def _print_suggestion(s: dict[str, Any]) -> None:
    click.echo(f"stack      : {s['stack']} (environment {s['endpoint_id']}, {s['kind']})")
    click.echo(f"slug       : {s['slug']}")
    click.echo(f"under      : {s['parent'] or '(choose one with --parent)'}")
    if s["parents"]:
        click.echo("candidates : " + ", ".join(f"{p['slug']} ({p['guest']})" for p in s["parents"]))
    for note in s["notes"]:
        click.secho(f"note       : {note}", fg="yellow")
    click.echo("containers :")
    for c in s["children"]:
        tag = "  [database: backup waits for it]" if c["likely_db"] else ""
        click.echo(f"  {c['container']:<36} -> check container:{c['container']}{tag}")
    click.echo(
        "proxy hosts pointing at it:" if s["hosts"] else "proxy hosts : none match its ports"
    )
    for h in s["hosts"]:
        where = f"already on {h['linked_to']}" if h["linked_to"] else "unlinked"
        click.echo(f"  {h['hostname']:<36} {h['upstream']}  ({where})")
    click.echo("probes it would add:")
    for p in s["probes"]:
        click.echo(
            f"  {p['service']:<24} {p['kind']:<10} {'required' if p['required'] else 'optional'}"
        )


@stack.command("suggest")
@click.argument("endpoint", type=int)
@click.argument("name")
@click.option("--parent", default=None, help="The service (guest) it lives under.")
@click.option("--json", "as_json", is_flag=True)
def stack_suggest(endpoint: int, name: str, parent: str | None, as_json: bool) -> None:
    """What adopting NAME would create (reads only)."""
    instance = _instance()

    async def go(session: Session, adapters: AdapterBundle, settings: Settings) -> Any:
        found = await adoption.suggest(
            session, adapters, endpoint, name, parent=parent, instance=instance
        )
        return found.as_dict()

    data = _run(go)
    if as_json:
        click.echo(json.dumps(data, indent=2))
    else:
        _print_suggestion(data)


@stack.command("adopt")
@click.argument("endpoint", type=int)
@click.argument("name")
@click.option("--slug", default=None, help="Default: the suggested slug.")
@click.option("--parent", default=None, help="The service (guest) it lives under.")
@click.option("--group", default="")
@click.option("--no-children", is_flag=True, help="Do not add a check per container.")
@click.option("--host", "hosts", multiple=True, help="Link this proxy host (repeatable).")
@click.option("--all-hosts", is_flag=True, help="Link every unlinked proxy host that points at it.")
@click.option("--no-probes", is_flag=True)
@click.option(
    "--deploy",
    "deploy_preset",
    type=click.Choice(["none", "probes", "probes+backup"]),
    default=None,
)
@click.option("--webhook", type=click.Choice(["generic", "gitlab", "github"]), default=None)
@click.option("--yes", is_flag=True, help="Do not ask first.")
def stack_adopt(
    endpoint: int,
    name: str,
    slug: str | None,
    parent: str | None,
    group: str,
    no_children: bool,
    hosts: tuple[str, ...],
    all_hosts: bool,
    no_probes: bool,
    deploy_preset: str | None,
    webhook: str | None,
    yes: bool,
) -> None:
    """Link a Portainer stack as a sub-service, with a child per container."""
    instance = _instance()

    async def go(session: Session, adapters: AdapterBundle, settings: Settings) -> Any:
        s = await adoption.suggest(
            session, adapters, endpoint, name, parent=parent, instance=instance
        )
        click.echo("")
        _print_suggestion(s.as_dict())
        chosen_hosts = list(hosts) or (
            [h["hostname"] for h in s.hosts if not h["linked_to"]] if all_hosts else []
        )
        plan = AdoptPlan(
            endpoint_id=endpoint,
            stack=s.stack,
            instance=s.instance,
            slug=slug or s.slug,
            parent=parent or s.parent or "",
            name=s.name,
            group=group,
            children=(
                []
                if no_children
                else [
                    ChildPlan(c["container"], c["slug"], c["name"], c["backup_gate"])
                    for c in s.children
                ]
            ),
            hosts=chosen_hosts,
            probes=not no_probes,
            deploy=deploy_preset,
            webhook=webhook,
        )
        if not yes:
            click.echo(
                f"\nCreate {plan.slug!r} under {plan.parent!r} with {len(plan.children)} container(s), "
                f"{len(plan.hosts)} proxy host(s)?"
            )
            click.confirm("Go ahead", abort=True)
        return await adoption.adopt(session, adapters, settings, "cli", plan)

    result = _run(go)
    click.secho(f"Linked: {', '.join(result.services)}", fg="green")
    if result.hosts:
        click.echo(
            f"proxy hosts: {', '.join(result.hosts)} (push them to Traefik Manager when ready)"
        )
    click.echo(f"probes added: {result.probes}")
    if result.webhook_secret:
        click.secho("Webhook secret (copy it now; not shown again):", bold=True)
        click.echo(f"  {result.webhook_secret}")
    click.echo("The registry has unsaved changes: save them to services.yaml from the dashboard.")


def _env(pairs: tuple[str, ...], env_file: Path | None) -> tuple[tuple[str, str], ...]:
    out: dict[str, str] = {}
    if env_file is not None:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip().strip('"').strip("'")
    for pair in pairs:
        if "=" not in pair:
            raise click.BadParameter(f"{pair!r} is not KEY=VALUE", param_hint="--env")
        key, _, value = pair.partition("=")
        out[key.strip()] = value
    return tuple(out.items())


@stack.command("create")
@click.argument("endpoint", type=int)
@click.argument("name")
@click.option("--git", "repository_url", default="", help="Git repository URL.")
@click.option("--ref", "reference", default="refs/heads/main", show_default=True)
@click.option("--compose-path", default="docker-compose.yml", show_default=True)
@click.option(
    "--credential",
    "git_credential_id",
    type=int,
    default=None,
    help="A Git credential Portainer already has (its id).",
)
@click.option("--git-username", default="")
@click.option(
    "--git-password", default="", help="Sent to Portainer and not kept; prefer --credential."
)
@click.option("--auto-update", is_flag=True, help="Let Portainer poll the repository.")
@click.option(
    "--compose-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Create from this compose file instead of Git.",
)
@click.option(
    "--env", "env_pairs", multiple=True, help="KEY=VALUE, sent to Portainer and not kept."
)
@click.option(
    "--env-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None
)
@click.option("--adopt-as", default=None, help="Also link it as this sub-service.")
@click.option("--parent", default=None, help="With --adopt-as: the service it lives under.")
@click.option("--yes", is_flag=True, help="Do not ask first.")
def stack_create(
    endpoint: int,
    name: str,
    repository_url: str,
    reference: str,
    compose_path: str,
    git_credential_id: int | None,
    git_username: str,
    git_password: str,
    auto_update: bool,
    compose_file: Path | None,
    env_pairs: tuple[str, ...],
    env_file: Path | None,
    adopt_as: str | None,
    parent: str | None,
    yes: bool,
) -> None:
    """Create a stack in Portainer (from Git or a compose file). The request is shown first."""
    instance = _instance()
    if bool(repository_url) == bool(compose_file):
        raise click.UsageError("give exactly one of --git and --compose-file")
    spec = StackCreate(
        endpoint_id=endpoint,
        name=name,
        source="string" if compose_file else "git",
        repository_url=repository_url,
        reference=reference,
        compose_path=compose_path,
        git_credential_id=git_credential_id,
        git_username=git_username,
        git_password=git_password,
        auto_update=auto_update,
        compose_text=compose_file.read_text() if compose_file else "",
        env=_env(env_pairs, env_file),
    )

    async def go(session: Session, adapters: AdapterBundle, settings: Settings) -> Any:
        request = stack_ops.preview(adapters, spec, instance)
        click.echo("This is what will be sent to Portainer (secrets masked):")
        click.echo(json.dumps(request, indent=2))
        if not yes:
            click.confirm("Create it", abort=True)
        plan = None
        if adopt_as:
            s_parent = parent or ""
            plan = AdoptPlan(
                endpoint_id=endpoint, stack=name, slug=adopt_as, parent=s_parent, name=name
            )
        return await stack_ops.create(
            session, adapters, settings, "cli", spec, adopt_plan=plan, instance=instance
        )

    result = _run(go)
    click.secho(f"Created stack {result.stack.name} in Portainer.", fg="green")
    for w in result.warnings:
        click.secho(w, fg="yellow")
    if result.adopted:
        click.echo(f"Linked: {', '.join(result.adopted.services)}")
    else:
        click.echo(f"Link it here with:  stack adopt {endpoint} {name}")


@stack.command("delete")
@click.argument("endpoint", type=int)
@click.argument("name")
@click.option("--confirm-name", default=None, help="Skip the prompt by typing the name here.")
@click.option(
    "--take-down", is_flag=True, help="Switch linked services off first, in the right order."
)
@click.option(
    "--keep-services", is_flag=True, help="Keep the sub-services (only the link is cleared)."
)
def stack_delete(
    endpoint: int, name: str, confirm_name: str | None, take_down: bool, keep_services: bool
) -> None:
    """Delete the stack in Portainer (its volumes are kept), then the link."""
    instance = _instance()
    typed = (
        confirm_name
        if confirm_name is not None
        else click.prompt(f"Type the stack's name ({name}) to delete it")
    )

    async def go(session: Session, adapters: AdapterBundle, settings: Settings) -> Any:
        return await stack_ops.delete(
            session, adapters, settings, "cli", endpoint, name,
            confirm_name=typed, take_down=take_down, remove_services=not keep_services,
            instance=instance,
        )  # fmt: skip

    result = _run(go)
    click.secho(f"Deleted stack {result.stack} (volumes kept).", fg="green")
    for label, items in (
        ("taken down", result.taken_down),
        ("services removed", result.removed_services),
        ("proxy hosts now unlinked", result.unlinked_hosts),
    ):
        if items:
            click.echo(f"{label}: {', '.join(items)}")
    for w in result.warnings:
        click.secho(w, fg="yellow")


def _control(kind: str, endpoint: int, ref: str, action: str) -> None:
    async def go(session: Session, adapters: AdapterBundle, settings: Settings) -> Any:
        return await stack_ops.control(
            session,
            adapters,
            "cli",
            kind=kind,
            endpoint_id=endpoint,
            ref=ref,
            action=action,
            instance=_instance(),
        )

    click.echo(_run(go))


for _action in ("start", "stop", "restart"):

    def _make(action: str) -> Any:
        @stack.command(action, help=f"{action.capitalize()} a stack in Portainer.")
        @click.argument("endpoint", type=int)
        @click.argument("name")
        def stack_cmd(endpoint: int, name: str) -> None:
            _control("stack", endpoint, name, action)

        @stack.command(
            f"{action}-container", help=f"{action.capitalize()} one container in Portainer."
        )
        @click.argument("endpoint", type=int)
        @click.argument("container")
        def container_cmd(endpoint: int, container: str) -> None:
            _control("container", endpoint, container, action)

        return stack_cmd

    _make(_action)


@service.command("link-host")
@click.argument("slug")
@click.argument("hostname")
@click.option("--force", is_flag=True, help="Move it from the service it is on now.")
def service_link_host(slug: str, hostname: str, force: bool) -> None:
    """Link proxy host HOSTNAME to service SLUG."""

    async def go(session: Session, adapters: AdapterBundle, settings: Settings) -> Any:
        svc = session.exec(select(ServiceRow).where(ServiceRow.slug == slug)).first()
        ph = session.exec(select(ProxyHost).where(ProxyHost.hostname == hostname)).first()
        if svc is None:
            raise StackError(f"no service {slug!r}", 404)
        if ph is None:
            raise StackError(f"no proxy host {hostname!r}", 404)
        if ph.service_id not in (None, svc.id) and not force:
            raise StackError(
                f"{hostname} is linked to another service - use --force to move it", 409
            )
        adoption.link_host(session, settings, "cli", ph, svc)
        session.commit()

    _run(go)
    click.secho(
        f"{hostname} is now linked to {slug}. Push it to Traefik Manager when ready.", fg="green"
    )


@service.command("unlink-host")
@click.argument("slug")
@click.argument("hostname")
def service_unlink_host(slug: str, hostname: str) -> None:
    """Unlink proxy host HOSTNAME from service SLUG (the host itself stays)."""

    async def go(session: Session, adapters: AdapterBundle, settings: Settings) -> Any:
        svc = session.exec(select(ServiceRow).where(ServiceRow.slug == slug)).first()
        ph = session.exec(select(ProxyHost).where(ProxyHost.hostname == hostname)).first()
        if svc is None or ph is None or ph.service_id != svc.id:
            raise StackError(f"{hostname} is not linked to {slug}", 409)
        adoption.link_host(session, settings, "cli", ph, None)
        session.commit()

    _run(go)
    click.secho(f"{hostname} is no longer linked to {slug}.", fg="green")
