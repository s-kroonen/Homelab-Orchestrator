"""Operator CLI. Commands group by what they touch:

    Set up      init                      clean install: writes .env + services.yaml
                config show | edit SECT   change one section of .env
                scaffold                  pull new/moved guests into services.yaml
    Registry    service | probe | proxy   edit entries in services.yaml
                proxy resync-maintenance  backfill the Traefik maintenance middleware (DB, not YAML)
    Diagnose    check                     can I reach Proxmox and PBS?
                transport check           is the host runner / ssh plumbing usable?
                guests | snapshots        what do Proxmox and PBS see?
                scan SLUG | --all         what will the backup gate decide?
                probe-test SLUG NAME      run one probe now, show its raw result
    Operate     backup SLUG               wake what it needs, then run one backup end to end
                backup plan SLUG          preview what a backup would wake (exit 1 on a blocker)
                wake SLUG                 wake the node, start the guest, wait until healthy
    Recover     admin reset               break-glass: wipe admins/passkeys, reopen signup

Every command that acts on infrastructure honours ``DRY_RUN``. Setup discovery
only reads, so it contacts the real cluster either way.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import click
from sqlmodel import Session, select

from orchestrator.adapters.errors import AdapterError, AdapterTlsError, AdapterUnreachable
from orchestrator.adapters.factory import build_adapters
from orchestrator.cli_config import policy as policy_group
from orchestrator.cli_config import probe, proxy
from orchestrator.cli_config import service as service_group
from orchestrator.cli_setup import config_group, init, scaffold
from orchestrator.config import get_settings
from orchestrator.db.models import Admin, Service, WebAuthnCredential
from orchestrator.db.models import Probe as ProbeRow
from orchestrator.db.session import build_engine, get_engine
from orchestrator.health.engine import HealthEngine
from orchestrator.logging_setup import configure_logging
from orchestrator.pipelines.backup import BackupError, BackupPipeline
from orchestrator.pipelines.wake import WakeError, WakePipeline
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


@click.group()
def admin_group() -> None:
    """Dashboard admin accounts (passkeys)."""


@admin_group.command("reset")
@click.option("--yes", is_flag=True, help="Skip the interactive confirmation.")
def admin_reset(yes: bool) -> None:
    """Break-glass: remove every admin + passkey, then re-open first-admin signup.

    Use this when every registered device/passkey is lost and nobody can log
    in to the dashboard. The CLI itself works without any of this — it's the
    break-glass path precisely for when the dashboard is unreachable. After
    this runs, the next visitor to /login can register a fresh first admin
    with no invite link needed, same as a brand-new install.
    """
    _bootstrap()
    with Session(get_engine()) as session:
        admins = session.exec(select(Admin)).all()
        if not admins:
            click.echo("No admins registered — nothing to reset.")
            return

        click.secho(
            f"This permanently removes {len(admins)} admin(s) and all their passkeys:", fg="yellow"
        )
        for a in admins:
            click.echo(f"  - {a.username}")
        if not yes and not click.confirm("Continue?", default=False):
            click.echo("Aborted.")
            return

        # Delete + flush credentials before admins: without an ORM-level
        # relationship() between the two, SQLAlchemy doesn't know they're
        # linked and may order the DELETEs so the DB's own ON DELETE CASCADE
        # removes a credential row first, then warns when its own DELETE
        # for that row matches nothing.
        for a in admins:
            for cred in session.exec(
                select(WebAuthnCredential).where(WebAuthnCredential.admin_id == a.id)
            ).all():
                session.delete(cred)
        session.flush()
        for a in admins:
            session.delete(a)
        session.commit()
        click.secho("Done. The dashboard will accept a fresh first-admin registration.", fg="green")


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
                except AdapterTlsError as exc:
                    # Checked before AdapterUnreachable — it is a subclass, and
                    # labelling a cert problem "unreachable" contradicts the
                    # message and misdirects the fix.
                    click.secho(f"  FAIL  {label}: TLS — {exc}", fg="red")
                    failures += 1
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
                # A datastore failure is usually an ACL problem, and PBS will
                # tell us plainly. Ask it rather than making the operator guess.
                await _diagnose_pbs_permissions(adapters, settings)
        finally:
            await adapters.stop_all()
        return failures

    failures = _run(_go())
    if failures:
        raise SystemExit(1)
    click.secho("\nAll checks passed.", fg="green", bold=True)


def _token_owner(token_id: str) -> str:
    """'orchestrator@pbs!datastore' -> 'orchestrator@pbs'."""
    return token_id.split("!", 1)[0]


async def _diagnose_pbs_permissions(adapters: Any, settings: Any) -> None:
    """Explain a PBS datastore failure using PBS's own permission report.

    An empty permission set means the token resolves to no privileges. With
    privilege separation, a token's effective privileges are the INTERSECTION of
    the owning user's ACL and the token's own ACL — so an ACL on only one of the
    two yields nothing, no matter how generous the role is.
    """
    try:
        perms = await adapters.pbs.effective_permissions()
    except AdapterError:
        return

    owner = _token_owner(settings.pbs_token_id)
    click.echo("")
    if not perms:
        click.secho("  DIAGNOSIS: this token resolves to NO permissions.", fg="yellow", bold=True)
        lines = [
            "    With privilege separation, a token's effective privileges are the",
            "    INTERSECTION of the owning USER's ACL and the TOKEN's own ACL.",
            "    An ACL on only one of the two intersects to nothing.",
            "",
            "    Check which one you are missing:",
            "      proxmox-backup-manager acl list",
            "",
            f"    You need a row for BOTH '{owner}' and '{settings.pbs_token_id}':",
            "",
            f"      proxmox-backup-manager acl update /datastore/{settings.pbs_datastore} \\",
            "          DatastoreAdmin \\",
            f"          --auth-id '{owner}'",
            "",
            f"      proxmox-backup-manager acl update /datastore/{settings.pbs_datastore} \\",
            "          DatastoreAdmin \\",
            f"          --auth-id '{settings.pbs_token_id}'",
            "",
            "    Then re-run: orchestrator-cli check",
        ]
        click.echo("\n".join(lines))
    else:
        click.secho("  Token's effective permissions per PBS:", fg="yellow")
        for path, privs in sorted(perms.items()):
            granted = ", ".join(sorted(k for k, v in privs.items() if v)) or "(none)"
            click.echo(f"    {path}: {granted}")
        click.echo(
            f"\n    Needed on /datastore/{settings.pbs_datastore}: "
            "Datastore.Audit (read), Datastore.Verify, Datastore.Prune."
        )


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
                # /cluster/resources filters by VM.Audit rather than returning
                # 403, so "no privileges" and "no guests" look identical. Say so.
                click.secho("(no guests returned)", fg="yellow")
                click.echo(
                    "\n  /cluster/resources filters silently by VM.Audit — an empty list\n"
                    "  usually means the token lacks it, not that the cluster is empty.\n"
                    "\n"
                    "  A token's effective privileges are the INTERSECTION of the owning\n"
                    "  user's ACL and the token's own ACL; an ACL on only one side grants\n"
                    "  nothing. Check both rows exist:\n"
                    "\n"
                    "      pveum acl list\n"
                )
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


class _BackupGroup(click.Group):
    """``backup SLUG`` keeps working exactly as before; ``backup plan SLUG`` is a
    subcommand. Anything that is not a subcommand name is routed to ``run`` with
    its arguments intact, so options may still precede the slug. (A service
    literally slugged ``plan`` must be backed up with ``backup run plan``.)"""

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args and args[0] in self.commands:
            return super().resolve_command(ctx, args)
        return "run", self.commands["run"], args


@cli.group(
    cls=_BackupGroup,
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
def backup() -> None:
    """Run a backup (`backup SLUG`) or preview what it would wake (`backup plan SLUG`)."""


@backup.command("run")
@click.argument("slug")
@click.option("--no-verify", is_flag=True, help="Skip the PBS verify step.")
@click.option(
    "--no-gate",
    is_flag=True,
    help="Bypass the integrity gate. Takes a backup WITHOUT verifying health first.",
)
@click.option(
    "--no-wake",
    is_flag=True,
    help=(
        "Skip the wake phase: do not wake the backup's prerequisites or the service "
        "itself. Use when you have brought everything up by hand. Recorded in the audit log."
    ),
)
def backup_run(slug: str, no_verify: bool, no_gate: bool, no_wake: bool) -> None:
    """Run a backup for SLUG end to end.

    First it wakes what the backup needs (its policy's and its own `requires`,
    then the service itself; see `backup plan SLUG`) and holds those nodes awake
    until it finishes. Then the integrity gate runs: only a HEALTHY verdict
    proceeds. Use `orchestrator-cli scan SLUG` to see what the gate will decide.
    """
    _bootstrap()
    settings = get_settings()
    adapters = build_adapters(settings)

    if settings.dry_run:
        click.secho("DRY_RUN is on — no real backup will be taken.", fg="yellow")
    if no_gate:
        click.secho(
            "WARNING: --no-gate bypasses the integrity check. This backup may "
            "capture a broken state over a known-good one. Recorded in the audit log.",
            fg="red",
            bold=True,
        )
    if no_wake:
        click.secho(
            "WARNING: --no-wake skips waking prerequisites and the service. The backup "
            "fails if anything it needs is off. Recorded in the audit log.",
            fg="red",
            bold=True,
        )

    async def _go() -> None:
        await adapters.start_all()
        try:
            pipeline = BackupPipeline(
                proxmox=adapters.proxmox,
                pbs=adapters.pbs,
                settings=settings,
                power=adapters.power,
            )
            with Session(get_engine()) as session:
                if settings.services_yaml_path.exists():
                    reconcile_yaml_into_db(session, settings.services_yaml_path)
                    session.commit()
                run = await pipeline.run_for_service(
                    session,
                    slug,
                    actor="cli",
                    verify=not no_verify,
                    gate=not no_gate,
                    wake=not no_wake,
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


@backup.command("plan")
@click.argument("slug")
@click.option(
    "--policy",
    "policy_name",
    default=None,
    help="Preview under this policy instead of the service's own.",
)
def backup_plan_cmd(slug: str, policy_name: str | None) -> None:
    """Print what a backup of SLUG would wake, in order.

    Nothing is woken. Exits non-zero if any step could never be woken (its node
    has no power target and is not marked always-on) so a script or a pre-flight
    check can refuse to schedule it.
    """
    from orchestrator.db.models import BackupPolicy
    from orchestrator.registry.backup_plan import resolve_backup_plan

    _bootstrap()
    settings = get_settings()
    with Session(get_engine()) as session:
        if settings.services_yaml_path.exists():
            reconcile_yaml_into_db(session, settings.services_yaml_path)
            session.commit()
        service = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
        if service is None:
            raise click.ClickException(f"no service registered with slug {slug!r}")
        if policy_name is not None:
            policy = session.exec(
                select(BackupPolicy).where(BackupPolicy.name == policy_name)
            ).one_or_none()
            if policy is None:
                raise click.ClickException(f"no backup policy named {policy_name!r}")
        else:
            policy = (
                session.get(BackupPolicy, service.backup_policy_id)
                if service.backup_policy_id
                else None
            )
        plan = resolve_backup_plan(session, service, policy)

    click.secho(f"Backup plan for {slug} (policy: {plan.policy or 'none'})", bold=True)
    if not plan.steps:
        click.echo("  nothing to wake (wake_before_backup is off and there are no prerequisites)")
    for i, step in enumerate(plan.steps, 1):
        line = (
            f"  {i}. {step.slug:<22} node {step.node or '?':<12} "
            f"{step.reason:<21} power: {step.node_power}"
        )
        click.secho(line, fg="red" if step.blocker else None)
        if step.blocker:
            click.secho(f"       BLOCKER: {step.blocker}", fg="red")
    if plan.blockers:
        click.secho(
            "\nThis backup would fail at the wake step. Give those nodes a power target "
            "or mark them always on.",
            fg="red",
            bold=True,
        )
        raise SystemExit(1)
    click.echo("\nOK — everything in the plan can be woken." if plan.will_wake else "\nOK.")


@cli.command()
@click.argument("slug")
@click.option("--timeout", "timeout_s", type=int, default=None, help="Override WAKE_TIMEOUT_S.")
def wake(slug: str, timeout_s: int | None) -> None:
    """Wake SLUG's node, start its guest, and wait for it to become healthy.

    Runs the same pipeline the web /wake endpoint fires — this just blocks
    until it finishes (or the timeout) instead of returning immediately.
    """
    _bootstrap()
    settings = get_settings()
    adapters = build_adapters(settings)

    if settings.dry_run:
        click.secho("DRY_RUN is on — no real power/proxmox action will be taken.", fg="yellow")

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
                run = await pipeline.run_for_service(
                    session, slug, actor="cli", timeout_s=timeout_s
                )
                click.echo("")
                click.secho(f"Run #{run.id} — {run.status.value}", fg="green", bold=True)
                for step in run.steps:
                    click.echo(f"  {step['status']:<13} {step['name']}")
        finally:
            await adapters.stop_all()

    try:
        _run(_go())
    except (WakeError, AdapterError) as exc:
        click.secho(f"\nWake failed: {exc}", fg="red", bold=True)
        raise SystemExit(1) from exc


@cli.command()
@click.argument("slug", required=False)
@click.option("--all", "scan_all", is_flag=True, help="Scan every enabled service.")
def scan(slug: str | None, scan_all: bool) -> None:
    """Run a service's health probes and show the verdict.

    This is what the backup gate consults. Run it before trusting a backup
    schedule: a service with no required probes reports UNKNOWN and will be
    refused, which is deliberate.
    """
    _bootstrap()
    settings = get_settings()

    if not slug and not scan_all:
        raise click.UsageError("give a SLUG or --all")

    async def _go() -> int:
        engine = HealthEngine(settings=settings)
        unhealthy = 0
        with Session(get_engine()) as session:
            if settings.services_yaml_path.exists():
                reconcile_yaml_into_db(session, settings.services_yaml_path)
                session.commit()

            stmt = select(Service).order_by(Service.slug)
            stmt = (
                stmt.where(Service.slug == slug)
                if slug
                else stmt.where(Service.enabled == True)  # noqa: E712
            )
            services = session.exec(stmt).all()

            if not services:
                click.secho(f"no service matched {slug!r}", fg="red")
                return 1

            colour = {"healthy": "green", "failed": "red", "unknown": "yellow"}
            for svc in services:
                verdict = await engine.scan(session, svc)
                state = verdict.state.value
                if state != "healthy":
                    unhealthy += 1
                click.secho(f"{svc.slug:<24} {state.upper()}", fg=colour[state], bold=True)
                click.echo(f"    {verdict.reason}")
                for r in verdict.probe_results:
                    mark = {"healthy": "ok  ", "failed": "FAIL", "unknown": "??  "}[r.state.value]
                    latency = f" ({r.latency_ms}ms)" if r.latency_ms is not None else ""
                    click.echo(f"      {mark} {r.probe_name} [{r.kind.value}]{latency}")
                    if r.message and r.state.value != "healthy":
                        click.echo(f"           {r.message}")
                click.echo("")
        return unhealthy

    unhealthy = _run(_go())
    if unhealthy:
        click.secho(f"{unhealthy} service(s) would be REFUSED by the backup gate.", fg="yellow")
        raise SystemExit(1)
    click.secho("All scanned services are HEALTHY.", fg="green", bold=True)


@cli.command("probe-test")
@click.argument("slug")
@click.argument("probe_name")
def probe_test(slug: str, probe_name: str) -> None:
    """Run ONE probe right now and print its raw result.

    Unlike ``scan``, this bypasses the aggregate verdict and persists
    nothing to ServiceState — it exists for debugging a probe that is
    reporting UNKNOWN or FAILED and you want to see exactly what happened
    (the request made, the status code, the raw error), not just the badge.
    """
    _bootstrap()
    settings = get_settings()

    async def _go() -> Any:
        engine = HealthEngine(settings=settings)
        with Session(get_engine()) as session:
            if settings.services_yaml_path.exists():
                reconcile_yaml_into_db(session, settings.services_yaml_path)
                session.commit()

            svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
            if svc is None:
                raise click.ClickException(f"no service {slug!r}")
            row = session.exec(
                select(ProbeRow).where(ProbeRow.service_id == svc.id, ProbeRow.name == probe_name)
            ).one_or_none()
            if row is None:
                raise click.ClickException(f"no probe {probe_name!r} on service {slug!r}")
            return await engine.run_probe(row)

    result = _run(_go())
    colour = {"healthy": "green", "failed": "red", "unknown": "yellow"}[result.state.value]
    click.secho(
        f"{result.probe_name} [{result.kind.value}] -> {result.state.value.upper()}",
        fg=colour,
        bold=True,
    )
    if result.latency_ms is not None:
        click.echo(f"  latency: {result.latency_ms}ms")
    if result.message:
        click.echo(f"  message: {result.message}")
    if result.details:
        click.echo("  details:")
        for key, value in result.details.items():
            click.echo(f"    {key}: {value}")


# ---------------------------------------------------------------------------
# transport setup / validation
# ---------------------------------------------------------------------------


@click.group()
def transport() -> None:
    """Check and configure how probes reach your guests."""


@transport.command("check")
@click.option("--host", default=None, help="Try reaching this specific host.")
@click.option(
    "--route",
    type=click.Choice(["host_agent", "ssh", "both"]),
    default="both",
    help="Which route to test.",
)
def transport_check(host: str | None, route: str) -> None:
    """Verify the plumbing probes depend on, before writing any probes.

    Checks the things that are easy to get wrong once and then puzzle over: is
    the host runner up and is its socket mounted, what will it actually allow,
    is the SSH key mounted, can the jump host be reached.
    """
    _bootstrap()
    settings = get_settings()
    problems = 0

    click.secho("Probe transport check", bold=True)
    click.echo(f"  dry_run                : {settings.dry_run}")
    if settings.dry_run:
        click.secho(
            "\n  DRY_RUN is on, so probes would use the recording transport and"
            "\n  never touch these paths. Re-run with DRY_RUN=false to test for real.",
            fg="yellow",
        )
    click.echo("")

    # ---- host runner ------------------------------------------------------
    if route in {"host_agent", "both"}:
        click.secho("Host runner route (recommended)", bold=True)
        sock = settings.host_runner_socket
        click.echo(f"  HOST_RUNNER_SOCKET     : {sock or '(unset)'}")
        click.secho(
            "  The runner executes checks on the HOST. This container holds no"
            "\n  inventory, no playbooks and no keys — only this socket.",
            fg="cyan",
        )
        if not sock:
            click.secho(
                "  note  not configured. Set HOST_RUNNER_SOCKET and mount the socket"
                "\n        in; see docs/host_runner.md.",
                fg="yellow",
            )
        else:
            problems += _probe_host_runner(sock, host)
        click.echo("")

    # ---- ssh --------------------------------------------------------------
    if route in {"ssh", "both"}:
        click.secho("Direct SSH route", bold=True)
        click.secho(
            "  Needs a private key mounted INTO this container. The host runner"
            "\n  avoids that; prefer it unless you have a reason not to.",
            fg="cyan",
        )
        click.echo(f"  SSH_KEY_PATH           : {settings.ssh_key_path or '(unset)'}")
        click.echo(f"  SSH_KNOWN_HOSTS_PATH   : {settings.ssh_known_hosts_path or '(unset)'}")
        click.echo(f"  SSH_VERIFY_HOST_KEY    : {settings.ssh_verify_host_key}")
        click.echo(f"  SSH_JUMP_HOST          : {settings.ssh_jump_host or '(none — direct)'}")

        if settings.ssh_key_path:
            if Path(settings.ssh_key_path).exists():
                click.secho("  OK    key is readable", fg="green")
            else:
                click.secho(
                    f"  FAIL  key not found at {settings.ssh_key_path!r} "
                    f"(mount it into the container)",
                    fg="red",
                )
                problems += 1
        else:
            click.secho("  note  no key configured — this route is unused.", fg="yellow")

        if settings.ssh_verify_host_key and not settings.ssh_known_hosts_path:
            click.secho(
                "  note  host-key verification is ON but no known_hosts is configured,"
                "\n        so every connection will fail until you mount one (or set"
                "\n        SSH_VERIFY_HOST_KEY=false, understanding the exposure).",
                fg="yellow",
            )

        if settings.ssh_jump_host:
            problems += _try_tcp(settings.ssh_jump_host, settings.ssh_jump_port, "jump host")
        click.echo("")

    if problems:
        click.secho(f"{problems} problem(s) found.", fg="red", bold=True)
        raise SystemExit(1)
    click.secho("Transport plumbing looks usable.", fg="green", bold=True)


def _probe_host_runner(socket_path: str, host: str | None) -> int:
    """Ask the runner what it allows, then optionally ping a host through it."""
    from orchestrator.health.transports.base import TransportError
    from orchestrator.health.transports.host_agent import HostAgentTransport

    async def _go() -> int:
        problems = 0
        try:
            info = await HostAgentTransport(socket_path=socket_path).hello()
        except TransportError as exc:
            click.secho(f"  FAIL  {exc}", fg="red")
            return 1

        click.secho("  OK    host runner answered", fg="green")
        allow = info.get("allow", {})
        click.echo(f"        ping      : {allow.get('ping')}")
        click.echo(f"        playbooks : {', '.join(allow.get('playbooks') or []) or '(none)'}")
        click.echo(f"        commands  : {allow.get('command')}")
        click.echo(f"        hosts     : {', '.join(allow.get('hosts') or []) or '(none)'}")
        if allow.get("command"):
            click.secho(
                "  note  ad-hoc commands are ENABLED on the runner. That gives this"
                "\n        container arbitrary remote execution on every allowed host."
                "\n        Prefer allowlisted playbooks unless you need it.",
                fg="yellow",
            )

        if host:
            try:
                t = HostAgentTransport(socket_path=socket_path, host=host, action="ping")
                result = await t.run(["true"], timeout_s=60)
            except TransportError as exc:
                click.secho(f"  FAIL  ping {host}: {exc}", fg="red")
                return problems + 1
            if result.ok:
                click.secho(f"  OK    ping {host} answered", fg="green")
            else:
                click.secho(
                    f"  FAIL  reached {host} but ping did not succeed "
                    f"(exit {result.exit_code}): {result.tail()}",
                    fg="red",
                )
                problems += 1
        return problems

    return int(_run(_go()))


def _try_tcp(host: str, port: int, label: str) -> int:
    """Plain reachability. Returns 1 if it failed."""
    import socket

    try:
        socket.create_connection((host, port), timeout=5).close()
    except OSError as exc:
        click.secho(f"  FAIL  cannot reach {label} at {host}:{port} — {exc}", fg="red")
        return 1
    click.secho(f"  OK    {label} reachable at {host}:{port}", fg="green")
    return 0


# Setup: init writes everything, config changes .env, scaffold updates the registry.
cli.add_command(init)
cli.add_command(config_group, name="config")
cli.add_command(scaffold)
# Registry entries, without hand-editing YAML.
cli.add_command(probe)
cli.add_command(proxy)
cli.add_command(service_group, name="service")
cli.add_command(policy_group, name="policy")
cli.add_command(transport)
cli.add_command(admin_group, name="admin")


if __name__ == "__main__":
    cli()
