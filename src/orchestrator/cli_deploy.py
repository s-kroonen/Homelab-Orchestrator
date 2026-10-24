"""``orchestrator-cli deploy`` - how an application is deployed, what guards it, and running it.

The same settings as the service page's Deploy panel. Configuration is edited in the
registry file like every other service setting; running a deploy acts on the live system.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import click

from orchestrator.cli_config import open_editor, save
from orchestrator.deploy.policy import (
    ON_FAILURE,
    PRESETS,
    PROVIDERS,
    check_target,
    normalize_deploy,
    preset,
)


@click.group()
def deploy() -> None:
    """Deploy methods and guards: configure, run, history."""


def _current(slug: str, path_override: str | None) -> tuple[Any, dict[str, Any]]:
    editor = open_editor(path_override)
    editor.require_service(slug)
    svc = editor.get_service(slug) or {}
    return editor, json.loads(json.dumps(svc.get("deploy") or {}, default=dict))


def describe(cfg: dict[str, Any]) -> list[str]:
    """Plain-language lines for a normalised ``deploy`` block."""
    if not cfg:
        return ["no deploy method"]
    pre, post = cfg["guard"]["pre"], cfg["guard"]["post"]
    hook = cfg["trigger"]["webhook"]
    lines = [
        f"deploys          : {'enabled' if cfg['enabled'] else 'switched off'}",
        "trigger          : manual (UI / CLI)"
        + (
            f" + webhook ({hook['provider']}{', branch ' + hook['branch'] if hook['branch'] else ''})"
            if hook
            else ""
        ),
        f"before           : probes {'must be healthy' if pre['probes'] else 'not checked'}",
    ]
    if pre["backup"]:
        b = pre["backup"]
        lines.append(
            f"                   backup newer than {b['max_age_h']}h"
            + (", else take one" if b["take_if_older"] else ", else refuse")
        )
    else:
        lines.append("                   no backup required")
    lines.append(
        "after            : "
        + (
            f"probes healthy within {post['timeout_s']}s, then stay healthy {post['settle_s']}s"
            if post["probes"]
            else "stack running"
        )
    )
    lines.append(f"on failure       : {cfg['on_failure']}")
    lines.append(
        f"redeploy         : {'pull images' if cfg['pull_image'] else 'no image pull'}"
        + (", prune" if cfg["prune"] else "")
    )
    return lines


@deploy.command("set")
@click.argument("slug")
@click.option(
    "--preset",
    "preset_name",
    type=click.Choice(list(PRESETS)),
    default=None,
    help="Start from none, probes, or probes+backup (replaces the guards).",
)
@click.option(
    "--pre-probes/--no-pre-probes", default=None, help="Refuse unless probes are healthy."
)
@click.option(
    "--backup-max-age",
    type=int,
    default=None,
    help="Require a verified backup newer than this many hours.",
)
@click.option("--no-backup-guard", is_flag=True, help="Do not require a backup.")
@click.option("--take-backup/--no-take-backup", default=None, help="Take one when it is older.")
@click.option("--post-probes/--no-post-probes", default=None)
@click.option("--post-timeout", type=int, default=None, help="Seconds to become healthy.")
@click.option("--settle", type=int, default=None, help="Seconds it must then stay healthy.")
@click.option("--on-failure", type=click.Choice(list(ON_FAILURE)), default=None)
@click.option("--pull/--no-pull", "pull_image", default=None, help="Pull images on redeploy.")
@click.option("--prune/--no-prune", default=None)
@click.option(
    "--webhook",
    default=None,
    help=f"Accept a deploy webhook from: {', '.join(PROVIDERS)}; 'none' to turn it off.",
)
@click.option("--branch", default=None, help="Only deploy pushes to this branch ('' = any).")
@click.option("--enable/--disable", "enabled", default=None, help="Allow deploys at all.")
@click.option("--file", "path_override", default=None)
def deploy_set(
    slug: str,
    preset_name: str | None,
    pre_probes: bool | None,
    backup_max_age: int | None,
    no_backup_guard: bool,
    take_backup: bool | None,
    post_probes: bool | None,
    post_timeout: int | None,
    settle: int | None,
    on_failure: str | None,
    pull_image: bool | None,
    prune: bool | None,
    webhook: str | None,
    branch: str | None,
    enabled: bool | None,
    path_override: str | None,
) -> None:
    """Set SLUG's deploy method and guards. Only the options you pass change."""
    editor, cur = _current(slug, path_override)
    svc = editor.get_service(slug) or {}
    try:
        cfg = normalize_deploy(preset(preset_name) if preset_name else (cur or {"enabled": True}))
        cfg = copy.deepcopy(cfg)
        pre, post = cfg["guard"]["pre"], cfg["guard"]["post"]
        if pre_probes is not None:
            pre["probes"] = pre_probes
        if no_backup_guard:
            pre["backup"] = None
        if backup_max_age is not None or take_backup is not None:
            b = pre["backup"] or {"max_age_h": 24, "take_if_older": True}
            if backup_max_age is not None:
                b["max_age_h"] = backup_max_age
            if take_backup is not None:
                b["take_if_older"] = take_backup
            pre["backup"] = b
        if post_probes is not None:
            post["probes"] = post_probes
        if post_timeout is not None:
            post["timeout_s"] = post_timeout
        if settle is not None:
            post["settle_s"] = settle
        if on_failure is not None:
            cfg["on_failure"] = on_failure
        if pull_image is not None:
            cfg["pull_image"] = pull_image
        if prune is not None:
            cfg["prune"] = prune
        if enabled is not None:
            cfg["enabled"] = enabled
        if webhook is not None:
            cfg["trigger"]["webhook"] = (
                None
                if webhook.lower() in {"none", "off", ""}
                else {
                    "provider": webhook.lower(),
                    "branch": (cfg["trigger"]["webhook"] or {}).get("branch", ""),
                }
            )
        if branch is not None:
            hook = cfg["trigger"]["webhook"]
            if hook is None:
                raise ValueError("--branch needs a webhook (use --webhook gitlab, for example)")
            hook["branch"] = branch
        cfg = normalize_deploy(cfg)
        check_target(cfg, svc.get("controller") or {})
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    editor.upsert_service(slug, {"deploy": cfg})
    save(editor, f"deploy settings for {slug!r}")
    for line in describe(cfg):
        click.echo(f"  {line}")


@deploy.command("clear")
@click.argument("slug")
@click.option("--file", "path_override", default=None)
def deploy_clear(slug: str, path_override: str | None) -> None:
    """Remove SLUG's deploy method (the stack itself is not touched)."""
    editor, _ = _current(slug, path_override)
    (editor.get_service(slug) or {}).pop("deploy", None)
    save(editor, f"deploy method removed from {slug!r}")


# -- running ----------------------------------------------------------------------------------


def _print_run(run: Any) -> None:
    colour = {"succeeded": "green", "failed": "red", "skipped": "yellow"}.get(run.status.value)
    click.secho(f"Run #{run.id} - {run.status.value}", fg=colour, bold=True)
    for step in run.steps:
        detail = step.get("detail") or {}
        note = detail.get("error") or detail.get("reason") or detail.get("note") or ""
        click.echo(f"  {step['status']:<8} {step['name']:<14} {note}")
    if run.error:
        click.secho(f"  {run.error}", fg=colour)


def _run_deploy(
    slug: str, *, guards: bool, guards_only: bool, reason: str, actor: str = "cli"
) -> None:
    import asyncio

    from sqlmodel import Session

    from orchestrator.adapters.factory import build_adapters
    from orchestrator.config import get_settings
    from orchestrator.db.session import build_engine, get_engine
    from orchestrator.deploy.pipeline import DeployPipeline, DeployRefused, Trigger
    from orchestrator.domain.enums import PipelineStatus
    from orchestrator.logging_setup import configure_logging

    configure_logging()
    build_engine()
    settings = get_settings()
    adapters = build_adapters(settings)
    if settings.dry_run:
        click.secho("DRY_RUN is on - nothing real is deployed.", fg="yellow")

    async def go() -> Any:
        await adapters.start_all()
        try:
            with Session(get_engine()) as session:
                run = await DeployPipeline(adapters, settings).run_for_service(
                    session,
                    slug,
                    Trigger("cli", actor),
                    guards=guards,
                    guards_only=guards_only,
                    reason=reason,
                )
                session.refresh(run)
                session.expunge(run)
                return run
        finally:
            await adapters.stop_all()

    try:
        run = asyncio.run(go())
    except DeployRefused as exc:
        raise click.ClickException(str(exc)) from exc
    _print_run(run)
    if run.status is not PipelineStatus.SUCCEEDED:
        raise SystemExit(1)


@deploy.command("run")
@click.argument("slug")
@click.option(
    "--unguarded",
    is_flag=True,
    help="Skip the pre-guards (probes, backup). Needs --reason; it is audited.",
)
@click.option("--reason", default="", help="Why the guards are skipped.")
def deploy_run(slug: str, unguarded: bool, reason: str) -> None:
    """Redeploy SLUG's stack through the guards and wait for the result."""
    if unguarded and not reason.strip():
        raise click.UsageError("--unguarded needs --reason (it is written to the audit log)")
    _run_deploy(slug, guards=not unguarded, guards_only=False, reason=reason.strip())


@deploy.command("guards")
@click.argument("slug")
def deploy_guards(slug: str) -> None:
    """Run SLUG's pre-guards now and report, without deploying (a backup is not taken)."""
    _run_deploy(slug, guards=True, guards_only=True, reason="")


@deploy.command("history")
@click.argument("slug")
@click.option("--limit", type=int, default=10)
def deploy_history(slug: str, limit: int) -> None:
    """The latest deploys of SLUG."""
    from sqlmodel import Session, desc, select

    from orchestrator.db.models import PipelineRun, Service
    from orchestrator.db.session import build_engine, get_engine
    from orchestrator.domain.enums import PipelineKind

    build_engine()
    with Session(get_engine()) as session:
        svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
        if svc is None:
            raise click.ClickException(f"no service {slug!r}")
        runs = session.exec(
            select(PipelineRun)
            .where(PipelineRun.service_id == svc.id, PipelineRun.kind == PipelineKind.DEPLOY)
            .order_by(desc(PipelineRun.started_at))
            .limit(max(1, limit))
        ).all()
        if not runs:
            click.echo("No deploys yet.")
        for r in runs:
            source = next(
                (s["detail"].get("source", "") for s in r.steps if s["name"] == "trigger"), ""
            )
            click.echo(
                f"#{r.id:<5} {r.started_at:%Y-%m-%d %H:%M}  {r.status.value:<10} "
                f"{source:<8} {r.error or ''}"
            )


@deploy.command("show")
@click.argument("target")
@click.option("--json", "as_json", is_flag=True)
@click.option("--file", "path_override", default=None)
def deploy_show(target: str, as_json: bool, path_override: str | None) -> None:
    """Show a service's deploy settings (a SLUG), or one deploy run (a number)."""
    if target.isdigit():
        from sqlmodel import Session

        from orchestrator.db.models import PipelineRun
        from orchestrator.db.session import build_engine, get_engine

        build_engine()
        with Session(get_engine()) as session:
            run = session.get(PipelineRun, int(target))
            if run is None:
                raise click.ClickException(f"no run {target}")
            if as_json:
                click.echo(json.dumps(run.steps, indent=2))
            else:
                _print_run(run)
        return
    _, cfg = _current(target, path_override)
    cfg = normalize_deploy(cfg)
    if as_json:
        click.echo(json.dumps(cfg, indent=2))
        return
    for line in describe(cfg):
        click.echo(line)
