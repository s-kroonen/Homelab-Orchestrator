"""Setup commands: ``init``, ``config`` and ``scaffold``.

Each owns one job, so no two of them overlap:

    init              A clean install. Writes .env AND services.yaml from nothing,
                      replacing both.
    config edit X     Changes one section of .env — the Proxmox connection, the
                      jump host, ... Leaves the rest of .env and the registry alone.
    scaffold          The update command. Pulls cluster changes into services.yaml:
                      new guests, moved guests, new nodes. Never touches .env.

Three rules hold for all of them:

* **Checked before written.** Values are tried against the real cluster as they
  are entered, so a wrong token or datastore shows up in the wizard rather than in
  the first failed backup.
* **Nothing is written until the end.** Abandoning a wizard halfway leaves every
  file exactly as it was.
* **Anything replaced is copied aside first.** .env is git-ignored, so git cannot
  bring it back.

Discovery is read-only and deliberately ignores DRY_RUN. DRY_RUN governs what the
orchestrator *does*; listing nodes and datastores changes nothing, and a wizard
that could only show fake nodes would be useless.
"""

from __future__ import annotations

import asyncio
import secrets
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import click

from orchestrator.adapters.errors import AdapterError, AdapterTlsError, AdapterUnreachable
from orchestrator.adapters.pbs.api import LivePbsAdapter
from orchestrator.adapters.proxmox.api import LiveProxmoxAdapter
from orchestrator.adapters.proxmox.base import BackupStorage, ClusterStatus, Guest
from orchestrator.cli_config import (
    build_probe,
    choose_probe_kind,
    interactive_default,
    open_editor,
    parse_selection,
    probe_suggestions,
    registry_path,
    save,
    set_non_interactive,
)
from orchestrator.config import Settings, get_settings
from orchestrator.registry.editor import RegistryEditError, RegistryEditor
from orchestrator.setup.envfile import (
    ENV_SECTIONS,
    EnvFileError,
    backup_file,
    default_values,
    layout_names,
    read_env,
    render_generated,
    secret_names,
    write_env,
)

# ---------------------------------------------------------------------------
# discovery — read-only, live, DRY_RUN-independent
# ---------------------------------------------------------------------------


@dataclass
class ProxmoxFindings:
    version: str
    cluster: ClusterStatus
    storages: list[BackupStorage] = field(default_factory=list)


@dataclass
class PbsFindings:
    version: str
    datastores: list[str] = field(default_factory=list)


class LiveDiscovery:
    """Read-only lookups against the real cluster, whatever DRY_RUN says."""

    async def proxmox(self, settings: Settings) -> ProxmoxFindings:
        adapter = LiveProxmoxAdapter(settings)
        await adapter.start()
        try:
            version = await adapter.version()
            cluster = await adapter.cluster_status()
            try:
                storages = await adapter.list_backup_storages()
            except AdapterError:
                # Listing storage needs Datastore.Audit on /storage. Without it the
                # wizard still works; it asks for the storage ID instead.
                storages = []
            return ProxmoxFindings(version.version, cluster, storages)
        finally:
            await adapter.stop()

    async def pbs(self, settings: Settings) -> PbsFindings:
        adapter = LivePbsAdapter(settings)
        await adapter.start()
        try:
            version = await adapter.version()
            return PbsFindings(version.version, await adapter.list_datastores())
        finally:
            await adapter.stop()

    async def datastore(self, settings: Settings, name: str) -> None:
        adapter = LivePbsAdapter(settings)
        await adapter.start()
        try:
            await adapter.datastore_status(name)
        finally:
            await adapter.stop()

    async def guests(self, settings: Settings) -> tuple[list[Guest], ClusterStatus]:
        adapter = LiveProxmoxAdapter(settings)
        await adapter.start()
        try:
            return await adapter.list_guests(), await adapter.cluster_status()
        finally:
            await adapter.stop()

    def tcp(self, host: str, port: int) -> str | None:
        """None if a TCP connection succeeds, otherwise why it did not."""
        try:
            socket.create_connection((host, port), timeout=5).close()
        except OSError as exc:
            return str(exc)
        return None


#: Tests swap this for a fake that needs no network.
make_discovery: Callable[[], Any] = LiveDiscovery


class _IsolatedSettings(Settings):
    """Settings built ONLY from the values given — not .env, not the environment.

    The wizard tests the values being typed. Letting a stale .env or an inherited
    environment variable fill a gap would test something other than what gets written.
    """

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Any,
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        return (init_settings,)


def settings_from(values: dict[str, str]) -> Settings:
    fields = Settings.model_fields
    return _IsolatedSettings(
        **{k.lower(): v for k, v in values.items() if v != "" and k.lower() in fields}
    )


# ---------------------------------------------------------------------------
# the wizard
# ---------------------------------------------------------------------------

#: Returned by Wizard.check when the operator wants to re-enter the values.
RETRY = object()

_PLACEHOLDER_SECRETS = {"", "dev-only-change-me", "change-me-generate-a-long-random-string"}


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _heading(title: str, intro: str = "") -> None:
    click.secho(f"\n== {title} ==", bold=True)
    if intro:
        click.secho(intro, fg="cyan")


class Wizard:
    """Collects answers into a working copy of .env. Writes nothing itself."""

    def __init__(
        self,
        values: dict[str, str],
        *,
        interactive: bool,
        verify: bool,
        discovery: Any,
    ) -> None:
        self.values = values
        self.interactive = interactive
        self.verify = verify
        self.discovery = discovery
        self.cluster: ClusterStatus | None = None
        self.storages: list[BackupStorage] | None = None
        self.always_on: list[str] = []
        #: Node name -> the power manager's OWN device id (e.g. an MQTT topic
        #: segment like hp-ilo2). Frequently NOT the same string as the node name.
        self.power_targets: dict[str, str] = {}
        #: Node names to offer for power-target mapping: the live cluster's when
        #: known (init, config edit proxmox), else whatever the registry has
        #: (config edit power run alone).
        self.known_node_names: list[str] = []
        #: Service slugs, when a registry exists — lets `config edit network`
        #: offer the gateway from a list.
        self.registry_slugs: list[str] | None = None
        #: Warnings the operator carried on past; repeated before anything is written.
        self.problems: list[str] = []

    def settings(self) -> Settings:
        return settings_from(self.values)

    # -- asking ---------------------------------------------------------------

    def ask(
        self,
        key: str,
        prompt: str,
        *,
        help: str = "",
        secret: bool = False,
        required: bool = False,
        choices: list[str] | None = None,
    ) -> str:
        current = self.values.get(key, "")
        if not self.interactive:
            if required and not current:
                raise click.UsageError(
                    f"{key} has no value. Put it in the --from file, or run init interactively."
                )
            return current

        if help:
            click.secho(f"    {help}", fg="cyan")
        while True:
            if secret:
                hint = "set — Enter keeps it" if current else "not set"
                raw = click.prompt(
                    f"  {prompt} [{hint}]", default="", show_default=False, hide_input=True
                ).strip()
                raw = raw or current
            elif choices:
                raw = click.prompt(
                    f"  {prompt}", default=current or choices[0], type=click.Choice(choices)
                )
            else:
                raw = click.prompt(
                    f"  {prompt}", default=current, show_default=bool(current)
                ).strip()
                if raw == "-" and not required:
                    raw = ""  # an explicit clear, distinct from Enter-keeps-it
            if raw or not required:
                self.values[key] = raw
                return raw
            click.secho("    required", fg="red")

    def ask_int(self, key: str, prompt: str, *, help: str = "") -> int:
        while True:
            raw = self.ask(key, prompt, help=help, required=True)
            try:
                return int(raw)
            except ValueError:
                if not self.interactive:
                    raise click.UsageError(f"{key} must be a number, got {raw!r}") from None
                click.secho("    must be a number", fg="red")
                help = ""

    def ask_bool(self, key: str, prompt: str, *, help: str = "", default: bool = True) -> bool:
        current = self.values.get(key, "")
        value = _truthy(current) if current else default
        if self.interactive:
            if help:
                click.secho(f"    {help}", fg="cyan")
            value = click.confirm(f"  {prompt}", default=value)
        self.values[key] = "true" if value else "false"
        return value

    def choose_one(
        self,
        key: str,
        label: str,
        options: list[str],
        *,
        required: bool,
        help: str = "",
    ) -> str:
        """Pick from what the cluster reported, by number or name."""
        if not options:
            return self.ask(key, label, help=help, required=required)

        current = self.values.get(key, "")
        if not self.interactive:
            if not current and len(options) == 1:
                self.values[key] = current = options[0]
            if required and not current:
                raise click.UsageError(f"{key} has no value; choose one of: {', '.join(options)}")
            if current and current not in options:
                self.problems.append(f"{key}={current!r} is not among {', '.join(options)}")
            return current

        if help:
            click.secho(f"    {help}", fg="cyan")
        for i, option in enumerate(options, start=1):
            click.echo(f"  {i:>3}  {option}")
        default = current or (options[0] if len(options) == 1 else "")
        while True:
            raw = click.prompt(
                f"  {label} — number or name", default=default, show_default=bool(default)
            ).strip()
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                value = options[int(raw) - 1]
            elif raw in options or (
                raw
                and click.confirm(f"  {raw!r} is not in the list. Use it anyway?", default=False)
            ):
                value = raw
            elif not raw and not required:
                value = ""
            else:
                click.secho("    pick one from the list", fg="red")
                continue
            self.values[key] = value
            return value

    # -- checking -------------------------------------------------------------

    def check(self, label: str, make: Callable[[], Awaitable[Any]]) -> Any:
        """Run one live check. Returns its result, None if skipped, or RETRY."""
        if not self.verify:
            return None
        try:
            result = asyncio.run(make())
        except AdapterError as exc:
            if isinstance(exc, AdapterTlsError):
                what = "TLS certificate rejected"
            elif isinstance(exc, AdapterUnreachable):
                what = "unreachable"
            else:
                what = type(exc).__name__
            click.secho(f"  FAIL  {label}: {what}", fg="red")
            click.echo(f"        {exc}")
        else:
            click.secho(f"  OK    {label}", fg="green")
            return result

        if not self.interactive:
            raise click.ClickException(
                f"{label} check failed. Fix the value in the --from file, or pass "
                f"--no-verify to write it unchecked."
            )
        choice = click.prompt(
            "  [r]e-enter these values, [s]kip the check, [a]bort",
            type=click.Choice(["r", "s", "a"]),
            default="r",
            show_choices=False,
        )
        if choice == "a":
            raise click.Abort()
        if choice == "s":
            self.problems.append(f"{label}: not verified")
            return None
        return RETRY

    def check_tcp(self, label: str, host: str, port: int) -> None:
        """Reachability only, and a warning rather than a stop: the orchestrator
        often reaches these from a different network than the one running setup."""
        if not self.verify:
            return
        reason = self.discovery.tcp(host, port)
        if reason is None:
            click.secho(f"  OK    {label} reachable at {host}:{port}", fg="green")
            return
        message = f"cannot reach {label} at {host}:{port} from here — {reason}"
        click.secho(f"  WARN  {message}", fg="yellow")
        self.problems.append(message)


# ---------------------------------------------------------------------------
# sections — each edits one part of .env
# ---------------------------------------------------------------------------


def section_proxmox(w: Wizard) -> None:
    _heading(
        "Proxmox VE",
        "  Point this at a node that is ALWAYS ON. Proxmox proxies API calls across the\n"
        "  cluster, so one node reaches every guest — but if that node powers off, the\n"
        "  orchestrator loses the API exactly when it needs it to wake something.",
    )
    while True:
        w.ask("PROXMOX_HOST", "Proxmox host (ip or name)", required=True)
        w.ask_int("PROXMOX_PORT", "API port")
        w.ask_bool(
            "PROXMOX_VERIFY_TLS",
            "Verify its TLS certificate?",
            help="Answer no for Proxmox's default self-signed certificate.",
        )
        w.ask(
            "PROXMOX_TOKEN_ID",
            "API token id",
            help="user@realm!tokenname — required privileges: docs/api_tokens.md",
            required=True,
        )
        w.ask("PROXMOX_TOKEN_SECRET", "API token secret", secret=True, required=True)
        found = w.check("Proxmox API", lambda: w.discovery.proxmox(w.settings()))
        if found is not RETRY:
            break

    if found is not None:
        w.cluster = found.cluster
        w.storages = found.storages
        name = found.cluster.cluster_name
        click.echo(
            f"        Proxmox VE {found.version}, "
            + (f"cluster {name!r}" if name else "standalone node")
        )
    _choose_always_on(w)

    if w.interactive:
        click.secho("\n  Restore token — used only by the restore flow (phase 8)", bold=True)
    token = w.ask("PROXMOX_RESTORE_TOKEN_ID", "Restore token id (blank = not yet)")
    if token:
        w.ask("PROXMOX_RESTORE_TOKEN_SECRET", "Restore token secret", secret=True)


def _resolve_names(raw: str, names: list[str]) -> list[str]:
    chosen: list[str] = []
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        if part.isdigit() and 1 <= int(part) <= len(names):
            part = names[int(part) - 1]
        if part not in names:
            raise click.BadParameter(f"no node {part!r}; the cluster has {', '.join(names)}")
        if part not in chosen:
            chosen.append(part)
    return chosen


def _choose_always_on(w: Wizard) -> None:
    cluster = w.cluster
    if cluster is None:
        # Unverified: nothing to list, so take the names as typed.
        if w.interactive:
            raw = click.prompt(
                "  Always-on node name(s), comma separated",
                default=",".join(w.always_on),
                show_default=bool(w.always_on),
            )
            w.always_on = [p.strip() for p in raw.split(",") if p.strip()]
        return

    nodes = cluster.nodes
    names = [n.name for n in nodes]
    w.known_node_names = names
    click.echo(f"\n  {'#':>3}  {'NODE':<16} {'STATE':<8} {'IP':<16}")
    for i, n in enumerate(nodes, start=1):
        tag = "  <- PROXMOX_HOST reaches the API through this node" if n.local else ""
        state = "online" if n.online else "OFFLINE"
        click.echo(f"  {i:>3}  {n.name:<16} {state:<8} {n.ip:<16}{tag}")

    local = cluster.local_node
    current = [n for n in w.always_on if n in names] or ([local.name] if local else [])

    if w.interactive:
        click.secho(
            "    Always-on nodes are never powered off. The orchestrator relies on them to\n"
            "    reach the API and to wake everything else.",
            fg="cyan",
        )
        while True:
            raw = click.prompt(
                "  Always-on node(s) — numbers or names, comma separated",
                default=",".join(current),
            )
            try:
                chosen = _resolve_names(raw, names)
                break
            except click.BadParameter as exc:
                click.secho(f"    {exc.message}", fg="red")
    else:
        unknown = [n for n in w.always_on if n not in names]
        if unknown:
            raise click.UsageError(
                f"--always-on names nodes the cluster does not have: {', '.join(unknown)} "
                f"(it has {', '.join(names)})"
            )
        chosen = current
    w.always_on = chosen

    if local is not None and local.name not in chosen:
        message = (
            f"PROXMOX_HOST reaches the API through {local.name}, which is not always-on. "
            f"When it powers off the orchestrator loses Proxmox, and cannot wake anything."
        )
        click.secho(f"  WARN  {message}", fg="yellow")
        target = next((n for n in nodes if n.name in chosen and n.ip), None)
        if (
            target is not None
            and w.interactive
            and click.confirm(
                f"  Point PROXMOX_HOST at {target.name} ({target.ip}) instead?", default=True
            )
        ):
            previous = w.values["PROXMOX_HOST"]
            w.values["PROXMOX_HOST"] = target.ip
            found = w.check(
                f"Proxmox API via {target.name}", lambda: w.discovery.proxmox(w.settings())
            )
            if isinstance(found, ProxmoxFindings):
                w.cluster = found.cluster
            else:
                w.values["PROXMOX_HOST"] = previous
                w.problems.append(message)
        else:
            w.problems.append(message)

    if cluster.quorate is False:
        message = "the cluster reports it is NOT quorate right now"
        click.secho(f"  WARN  {message}", fg="yellow")
        w.problems.append(message)
    elif len(nodes) > 1 and len(chosen) < len(nodes) // 2 + 1:
        click.secho(
            f"  NOTE  {len(chosen)} of {len(nodes)} nodes are always-on; a majority is "
            f"{len(nodes) // 2 + 1}. Unless a QDevice or lowered expected votes covers it,\n"
            f"        the cluster loses quorum while the others are off, and Proxmox then\n"
            f"        refuses to start guests. See docs/proxmox_connectivity.md.",
            fg="yellow",
        )


def section_pbs(w: Wizard) -> None:
    _heading(
        "Proxmox Backup Server",
        "  The orchestrator reads, verifies and prunes backups here. Proxmox writes them\n"
        "  itself, through its own storage entry.",
    )
    while True:
        w.ask("PBS_HOST", "PBS host (ip or name)", required=True)
        w.ask_int("PBS_PORT", "API port")
        w.ask_bool(
            "PBS_VERIFY_TLS",
            "Verify its TLS certificate?",
            help="Answer no for PBS's default self-signed certificate.",
        )
        w.ask(
            "PBS_TOKEN_ID",
            "API token id",
            help="The ACL must exist for BOTH the user and the token — their privileges intersect.",
            required=True,
        )
        w.ask("PBS_TOKEN_SECRET", "API token secret", secret=True, required=True)
        found = w.check("PBS API", lambda: w.discovery.pbs(w.settings()))
        if found is not RETRY:
            break

    datastores: list[str] = []
    if found is not None:
        datastores = found.datastores
        click.echo(f"        PBS {found.version}")
        if not datastores:
            click.secho(
                "  WARN  PBS lists no datastores for this token. It filters the list by\n"
                "        Datastore.Audit, so this usually means a missing ACL. Check BOTH rows:\n"
                "        proxmox-backup-manager acl list",
                fg="yellow",
            )

    while True:
        store = w.choose_one("PBS_DATASTORE", "Datastore", datastores, required=True)
        if found is None:
            break
        result = w.check(
            f"datastore {store!r} readable",
            lambda store=store: w.discovery.datastore(w.settings(), store),
        )
        if result is not RETRY:
            break

    w.ask("PBS_NODE_NAME", "PBS node name", help='"localhost" unless the PBS install was renamed.')
    _choose_backup_storage(w)
    w.ask_bool(
        "PBS_PROTECT_ENABLED",
        "Pin the last verified backup? (needs Datastore.Modify)",
        help="Answer no if the token lacks Datastore.Modify: pins become logged no-ops, not 403s.",
    )


def _choose_backup_storage(w: Wizard) -> None:
    """PVE_BACKUP_STORAGE: the PVE storage ID that points at the chosen datastore.

    The single most confusing pair of settings, so work it out rather than ask.
    """
    if w.storages is None and w.verify:
        try:
            w.storages = asyncio.run(w.discovery.proxmox(w.settings())).storages
        except AdapterError:
            w.storages = []

    store = w.values.get("PBS_DATASTORE", "")
    storages = w.storages or []
    matching = [s for s in storages if s.datastore == store]
    if len(matching) > 1:
        on_host = [s for s in matching if s.server == w.values.get("PBS_HOST")]
        matching = on_host or matching

    if len(matching) == 1:
        w.values["PVE_BACKUP_STORAGE"] = matching[0].storage
        click.secho(
            f"  OK    Proxmox writes to {store!r} through storage {matching[0].storage!r} "
            f"(PVE_BACKUP_STORAGE)",
            fg="green",
        )
        return

    if storages and not matching:
        click.secho(
            f"  WARN  no Proxmox storage entry points at datastore {store!r}. PBS-type "
            f"storages Proxmox has:",
            fg="yellow",
        )
    options = [s.storage for s in (matching or storages)]
    w.choose_one(
        "PVE_BACKUP_STORAGE",
        "Proxmox storage ID for this datastore",
        options,
        required=True,
        help="`pvesm status` on a node lists it. Not necessarily the same as the datastore name.",
    )


def section_network(w: Wizard) -> None:
    _heading(
        "Reaching your guests",
        "  The orchestrator is usually not on the guests' network. Probes reach them\n"
        "  through the gateway: HTTP via its reverse proxy, commands via the host runner\n"
        "  or an SSH jump. See docs/probing_through_a_gateway.md.",
    )
    while True:
        url = w.ask(
            "PROBE_PROXY_BASE_URL",
            "Reverse proxy LOCAL address for HTTP probes (blank = probes go direct)",
            help="e.g. https://10.0.0.2 — probes connect here and route with a Host header",
        )
        if not url:
            break
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            w.check_tcp("reverse proxy", parsed.hostname, port)
            break
        if not w.interactive:
            raise click.UsageError(
                f"PROBE_PROXY_BASE_URL must look like https://10.0.0.2, got {url!r}"
            )
        click.secho("    needs a scheme and a host, like https://10.0.0.2", fg="red")

    if w.registry_slugs is not None:
        w.choose_one(
            "GATEWAY_SERVICE",
            "Gateway service (blank = none)",
            w.registry_slugs,
            required=False,
            help="New services get depends_on: [gateway], so its outage reads as UNKNOWN downstream.",
        )

    if w.interactive:
        click.secho("\n  Command probes (systemd, docker, databases, ansible)", bold=True)
    w.ask(
        "HOST_RUNNER_SOCKET",
        "Host runner socket inside the container (blank = not used)",
        help=(
            "Recommended: checks run on the HOST, so this container holds no keys. The usual\n"
            "    path is /run/orchestrator/host-runner.sock — setup: docs/host_runner.md"
        ),
    )

    direct = bool(w.values.get("SSH_KEY_PATH"))
    if w.interactive:
        direct = click.confirm(
            "  Also SSH directly from the container? (needs a private key mounted in)",
            default=direct,
        )
        if not direct:
            for key in ("SSH_KEY_PATH", "SSH_KNOWN_HOSTS_PATH", "SSH_JUMP_HOST", "SSH_JUMP_USER"):
                w.values[key] = ""
    if not direct:
        return

    w.ask("SSH_KEY_PATH", "Private key path INSIDE the container", required=True)
    w.ask("SSH_KNOWN_HOSTS_PATH", "known_hosts path inside the container")
    w.ask_bool("SSH_VERIFY_HOST_KEY", "Verify host keys?")
    jump = w.ask("SSH_JUMP_HOST", "Default jump host (blank = connect directly)")
    if jump:
        w.ask("SSH_JUMP_USER", "Jump host user")
        port = w.ask_int("SSH_JUMP_PORT", "Jump host SSH port")
        w.check_tcp("jump host", jump, port)


def section_power(w: Wizard) -> None:
    _heading(
        "Power manager (Home Assistant MQTT)",
        "  Used to wake nodes on demand and, later, for the greenlight/restore flow's\n"
        "  restart action. Blank host = skip for now; the power adapter stays a logged\n"
        "  no-op until this is filled in and DRY_RUN is off.",
    )
    host = w.ask("MQTT_HOST", "MQTT broker host (blank = skip for now)")
    if not host:
        return
    port = w.ask_int("MQTT_PORT", "Port")
    w.ask_bool("MQTT_TLS", "Use TLS?", default=False)
    w.ask("MQTT_USERNAME", "Username")
    w.ask("MQTT_PASSWORD", "Password", secret=True)
    w.ask(
        "MQTT_POWER_TOPIC_PREFIX",
        "Runtime topic prefix (NOT the HA discovery prefix)",
        help=(
            "e.g. ipmi-manager — commands publish to <prefix>/<target>/command. The HA "
            'discovery prefix ("homeassistant/ipmi-manager/...") is a different thing.'
        ),
    )
    w.check_tcp("MQTT broker", host, port)
    _choose_power_targets(w)


def _choose_power_targets(w: Wizard) -> None:
    """Map each Proxmox node to the power manager's OWN device id.

    These are frequently different strings — a manager built on IPMI/iLO names
    devices by hardware (hp-ilo2, supermicro), not by what Proxmox calls the
    node — and getting it wrong is silent: publishing a command to a topic
    nobody subscribes to does not raise an error, it just does nothing.
    """
    if not w.known_node_names:
        return
    click.secho(
        "\n  Power manager target per node — the id the manager itself uses (an MQTT "
        "topic\n  segment, e.g. hp-ilo2, supermicro). Check its Home Assistant MQTT "
        "integration\n  page if unsure; this is usually NOT the Proxmox node name.",
        fg="cyan",
    )
    for name in w.known_node_names:
        current = w.power_targets.get(name, name)
        if w.interactive:
            value = click.prompt(f"  {name} -> power manager target", default=current).strip()
        else:
            value = current
        w.power_targets[name] = value or name


def section_web(w: Wizard) -> None:
    _heading("Dashboard", "  Passkeys are bound to the hostname the dashboard is served on.")
    rp_id = w.ask(
        "WEBAUTHN_RP_ID",
        "Hostname the dashboard will be served on",
        help="e.g. orchestrator.home.example — passkeys only work over HTTPS or on localhost",
        required=True,
    )
    origin = w.values.get("WEBAUTHN_ORIGIN", "")
    if urlparse(origin).hostname != rp_id:
        w.values["WEBAUTHN_ORIGIN"] = (
            "http://localhost:8080" if rp_id == "localhost" else f"https://{rp_id}"
        )
    w.ask("WEBAUTHN_ORIGIN", "Full origin (scheme://host[:port])", required=True)

    if w.values.get("SESSION_SECRET", "") in _PLACEHOLDER_SECRETS:
        w.values["SESSION_SECRET"] = secrets.token_urlsafe(64)
        click.secho("  OK    generated a new session secret", fg="green")
    else:
        click.echo("        keeping the existing session secret")


def section_runtime(w: Wizard) -> None:
    _heading("Runtime")
    image = w.ask(
        "ORCHESTRATOR_IMAGE",
        "Image to run",
        help="Pin a version tag (…:1.2.0) rather than :latest — this is the control plane.",
        required=True,
    )
    if image.endswith(":latest") or ":" not in image.rsplit("/", 1)[-1]:
        click.secho(
            "  NOTE  unpinned image: an update can arrive during a power event", fg="yellow"
        )
    w.ask_bool(
        "DRY_RUN",
        "Start in dry-run mode?",
        help="Recommended until `orchestrator-cli check` passes: nothing is started or backed up.",
        default=True,
    )
    w.ask("LOG_LEVEL", "Log level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])


#: `config edit` choices, in the order `init` walks through them.
SECTIONS: dict[str, Callable[[Wizard], None]] = {
    "proxmox": section_proxmox,
    "pbs": section_pbs,
    "network": section_network,
    "power": section_power,
    "web": section_web,
    "runtime": section_runtime,
}


# ---------------------------------------------------------------------------
# registry helpers — shared by init and scaffold
# ---------------------------------------------------------------------------


def slugify(name: str) -> str:
    """Proxmox guest name -> registry slug."""
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in name)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "unnamed"


@dataclass
class Candidate:
    guest: Guest
    slug: str
    #: Already in services.yaml.
    known: bool


def build_candidates(guests: list[Guest], editor: RegistryEditor) -> list[Candidate]:
    """Match guests to registry entries by VMID first, name second.

    VMID first because a renamed VM is still the same service: matching by name
    would add a duplicate and strand the probes on the original entry.
    """
    by_guest: dict[tuple[str, int], str] = {}
    for svc in editor.doc.get("services") or []:
        if svc.get("guest_id") is not None:
            by_guest[(str(svc.get("guest_kind")), int(svc.get("guest_id")))] = str(svc.get("slug"))

    taken = set(editor.service_slugs())
    out: list[Candidate] = []
    for guest in sorted(guests, key=lambda g: (g.node, g.vmid)):
        slug = by_guest.get((guest.kind.value, guest.vmid))
        if slug is not None:
            out.append(Candidate(guest, slug, known=True))
            continue
        slug = slugify(guest.name or f"guest-{guest.vmid}")
        if slug in taken:
            slug = f"{slug}-{guest.vmid}"
        taken.add(slug)
        out.append(Candidate(guest, slug, known=False))
    return out


def choose_candidates(
    candidates: list[Candidate],
    include: str | None,
    exclude: str | None,
    *,
    interactive: bool,
    confirm: bool = True,
) -> list[Candidate]:
    """Pick guests to add. Flags win; otherwise prompt; non-interactive adds nothing."""
    by_key: dict[str, int] = {}
    for i, c in enumerate(candidates):
        by_key[c.slug] = i
        by_key[str(c.guest.vmid)] = i

    def resolve(raw: str) -> set[int]:
        picked: set[int] = set()
        for part in (p.strip() for p in raw.split(",")):
            if not part:
                continue
            if part.lower() == "all":
                picked.update(range(len(candidates)))
            elif part in by_key:
                picked.add(by_key[part])
            else:
                raise click.BadParameter(f"no guest matches {part!r} (use a slug or a VMID)")
        return picked

    if include is not None:
        selected = resolve(include)
    elif not interactive:
        selected = set()
    else:
        click.echo(f"\n  {'#':>3}  {'VMID':>6}  {'KIND':<4} {'NODE':<12} {'STATUS':<9} SLUG")
        for i, c in enumerate(candidates, start=1):
            g = c.guest
            click.echo(
                f"  {i:>3}  {g.vmid:>6}  {g.kind.value:<4} {g.node:<12} {g.status:<9} {c.slug}"
            )
        click.echo("\n  Select with numbers or ranges (1-5,8,12), 'all', or 'none'.")
        while True:
            try:
                raw = click.prompt("  Add", default="all")
                selected = parse_selection(raw, len(candidates))
                break
            except click.BadParameter as exc:
                click.secho(f"    {exc.message}", fg="red")

    if exclude:
        selected -= resolve(exclude)
    chosen = [candidates[i] for i in sorted(selected)]

    if chosen and interactive and include is None and confirm:
        click.echo("  " + ", ".join(c.slug for c in chosen))
        if not click.confirm(f"  Add these {len(chosen)} service(s)?", default=True):
            return []
    return chosen


def new_service_fields(candidate: Candidate, policy: str | None, gateway: str) -> dict[str, Any]:
    g = candidate.guest
    fields: dict[str, Any] = {
        "name": g.name or candidate.slug,
        "description": "",
        "node": g.node,
        "guest_kind": g.kind.value,
        "guest_id": g.vmid,
        "enabled": True,
        "backup_excluded": False,
        "backup_excluded_reason": "",
        "backup_policy": policy or None,
    }
    if gateway and candidate.slug != gateway:
        fields["depends_on"] = [gateway]
    return fields


def report_gate_readiness(editor: RegistryEditor) -> None:
    """Say plainly which services the backup gate will refuse.

    A service with no gating probe reports UNKNOWN and is refused. That is
    deliberate, but worth saying right after services are added — otherwise the
    first refused backup is a surprise.
    """
    blocked = [
        str(svc.get("slug"))
        for svc in (editor.doc.get("services") or [])
        if not svc.get("backup_excluded")
        and not any(p.get("required", True) for p in (svc.get("probes") or []))
    ]
    if not blocked:
        return
    click.secho(
        f"\n{len(blocked)} service(s) have no gating probe and will be REFUSED by the "
        f"backup gate (verdict UNKNOWN):",
        fg="yellow",
    )
    click.echo("    " + ", ".join(blocked[:12]) + (" ..." if len(blocked) > 12 else ""))
    click.echo("\n  Add one with:  orchestrator-cli probe add <slug>")


def _merge_previous(previous: dict[str, str]) -> dict[str, str]:
    """Defaults, overlaid with the previous file. An empty value in the old file
    does not wipe a default (an empty port would not even parse); unknown keys are
    kept whatever they hold."""
    values = default_values()
    for key, value in previous.items():
        if value != "" or key not in values:
            values[key] = value
    return values


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

_DIR_OPTION = click.option(
    "--dir",
    "project_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Directory holding .env and config/. In the setup container: /work, the default.",
)


@click.command()
@_DIR_OPTION
@click.option(
    "--from",
    "answers_file",
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
    default=None,
    help="Start from this env file's values instead of the existing .env.",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Never prompt: answers come from --from (or the existing .env) and the flags.",
)
@click.option(
    "--force", is_flag=True, help="Replace existing files without asking (still backed up)."
)
@click.option(
    "--no-verify",
    is_flag=True,
    help="Do not contact Proxmox or PBS. Values are written unchecked and no guests are added.",
)
@click.option("--always-on", default=None, help="Always-on node names, comma separated.")
@click.option("--include", default=None, help="Guests to add: slugs, VMIDs or 'all'.")
@click.option("--exclude", default=None, help="Guests to leave out.")
@click.option("--gateway", default=None, help="Slug of the gateway service.")
@click.option(
    "--policy",
    "policy_name",
    default="daily-frequent",
    show_default=True,
    help="Backup policy attached to every added service.",
)
def init(
    project_dir: Path,
    answers_file: Path | None,
    non_interactive: bool,
    force: bool,
    no_verify: bool,
    always_on: str | None,
    include: str | None,
    exclude: str | None,
    gateway: str | None,
    policy_name: str,
) -> None:
    """Clean install: write .env and services.yaml from scratch.

    Every value is checked against the live cluster as you enter it, and nothing
    is written until you confirm at the end. Existing files are REPLACED — their
    current versions are copied aside first.

    Afterwards, change one part with `config edit <section>` and pull in new
    guests with `scaffold`. Re-running init starts over.
    """
    set_non_interactive(non_interactive)
    interactive = interactive_default()
    env_path = project_dir / ".env"
    yaml_path = project_dir / "config" / "services.yaml"

    click.secho("homelab-orchestrator setup", bold=True)
    click.echo(f"  .env          : {env_path}")
    click.echo(f"  services.yaml : {yaml_path}")

    # An empty file is what `touch .env` leaves — compose will not start the setup
    # service until .env exists — so it is a clean install, not something to replace.
    existing = [
        p for p in (env_path, yaml_path) if p.is_file() and p.read_text(encoding="utf-8").strip()
    ]
    if existing and not force:
        click.secho("\n  These already exist and will be REPLACED:", fg="yellow")
        for p in existing:
            click.echo(f"    {p}")
        click.echo(
            "  Their current versions are copied aside first, but a replaced services.yaml\n"
            "  loses its probes. To change one part instead: `config edit <section>` for\n"
            "  .env, `scaffold` for new guests."
        )
        if not interactive:
            raise click.UsageError(
                "files exist; pass --force to replace them (they are backed up first)"
            )
        if not click.confirm("  Start over?", default=False):
            click.echo("Nothing changed.")
            return

    try:
        previous = read_env(answers_file or env_path)
        old_registry = RegistryEditor(yaml_path) if yaml_path.exists() else None
    except (EnvFileError, RegistryEditError) as exc:
        raise click.ClickException(str(exc)) from exc

    w = Wizard(
        _merge_previous(previous),
        interactive=interactive,
        verify=not no_verify,
        discovery=make_discovery(),
    )
    if always_on is not None:
        w.always_on = [p.strip() for p in always_on.split(",") if p.strip()]
    elif old_registry is not None:
        w.always_on = [
            str(n.get("name")) for n in old_registry.doc.get("nodes") or [] if n.get("always_on")
        ]
    if old_registry is not None:
        w.known_node_names = old_registry.node_names()
        w.power_targets = {
            str(n.get("name")): str(n.get("power_mgr_target"))
            for n in old_registry.doc.get("nodes") or []
            if n.get("power_mgr_target")
        }

    if interactive:
        click.secho(
            "\n  Enter keeps the value in [brackets]; '-' clears an optional one.\n"
            "  Ctrl+C at any point leaves every file untouched.",
            fg="cyan",
        )
    for run_section in SECTIONS.values():
        run_section(w)

    editor = _build_registry(w, yaml_path, include, exclude, gateway, policy_name)

    # Validate both files completely before either is touched.
    try:
        editor.validate()
        render_generated(w.values)
    except (RegistryEditError, EnvFileError) as exc:
        raise click.ClickException(f"nothing was written: {exc}") from exc

    _heading("Summary")
    for node in editor.doc.get("nodes") or []:
        mark = "always-on" if node.get("always_on") else "powers off"
        click.echo(f"  node     {node.get('name'):<16} {mark}")
    click.echo(f"  services {len(editor.service_slugs())}")
    click.echo(f"  gateway  {w.values.get('GATEWAY_SERVICE') or '(none)'}")
    click.echo(f"  dry-run  {w.values.get('DRY_RUN')}")
    if w.problems:
        click.secho("\n  Carried on past:", fg="yellow")
        for problem in w.problems:
            click.secho(f"    - {problem}", fg="yellow")

    if interactive and not click.confirm("\n  Write these files?", default=True):
        click.echo("Nothing written.")
        return

    env_backup = write_env(env_path, w.values)
    yaml_backup = backup_file(yaml_path)
    editor.save()

    click.secho(f"\nWrote {env_path} and {yaml_path}", fg="green", bold=True)
    for backup in (env_backup, yaml_backup):
        if backup is not None:
            click.echo(f"  previous version kept as {backup}")
    report_gate_readiness(editor)
    click.echo(
        "\nNext:\n"
        "  docker compose up -d                                     # (re)create with the new .env\n"
        "  docker compose exec orchestrator orchestrator-cli check  # confirm the connection\n"
        "  docker compose run --rm setup probe add <slug>           # a probe per service"
    )


def _build_registry(
    w: Wizard,
    yaml_path: Path,
    include: str | None,
    exclude: str | None,
    gateway: str | None,
    policy_name: str,
) -> RegistryEditor:
    _heading(
        "Services",
        "  Pick the guests the orchestrator should manage. Anything left out can be added\n"
        "  later with `scaffold`.",
    )
    editor = RegistryEditor(yaml_path, fresh=True)

    guests: list[Guest] = []
    fetched = w.check("guest list", lambda: w.discovery.guests(w.settings()))
    if isinstance(fetched, tuple):
        guests, cluster = fetched
        w.cluster = w.cluster or cluster

    node_names = [n.name for n in w.cluster.nodes] if w.cluster else []
    for name in node_names + [n for n in w.always_on if n not in node_names]:
        editor.ensure_node(
            name, always_on=name in w.always_on, power_mgr_target=w.power_targets.get(name)
        )
    editor.ensure_policy(policy_name, schedule_cron="0 3 * * *", retention={"keep_daily": 7})

    candidates = build_candidates(guests, editor)
    if not candidates:
        click.secho("  No guests to add — run `scaffold` once the connection works.", fg="yellow")
    chosen = choose_candidates(candidates, include, exclude, interactive=w.interactive)

    for c in chosen:
        editor.ensure_node(c.guest.node)
    gateway_slug = _choose_gateway(w, chosen, gateway)
    w.values["GATEWAY_SERVICE"] = gateway_slug
    for c in chosen:
        editor.upsert_service(c.slug, new_service_fields(c, policy_name, gateway_slug))

    base = w.values.get("PROBE_PROXY_BASE_URL", "")
    if gateway_slug and base:
        parsed = urlparse(base)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        add = not w.interactive or click.confirm(
            f"  Give {gateway_slug} a TCP probe on the proxy ({parsed.hostname}:{port})? It is\n"
            f"  the one host reachable directly, which makes it a sound gating check",
            default=True,
        )
        if add:
            editor.upsert_probe(
                gateway_slug,
                {
                    "name": "proxy-listening",
                    "kind": "tcp",
                    "required": True,
                    "timeout_s": 5,
                    "order": 10,
                    "config": {"host": parsed.hostname, "port": port},
                },
            )
    return editor


def _choose_gateway(w: Wizard, chosen: list[Candidate], preset: str | None) -> str:
    slugs = [c.slug for c in chosen]
    if preset is not None:
        if preset and preset not in slugs:
            raise click.UsageError(f"--gateway {preset!r} is not one of the added services")
        return preset
    current = w.values.get("GATEWAY_SERVICE", "")
    current = current if current in slugs else ""
    if not w.interactive or not slugs:
        return current

    click.secho(
        "\n    The gateway runs the reverse proxy; everything else is reached through it.\n"
        "    Other services get depends_on: [gateway], so a gateway outage reads as\n"
        "    UNKNOWN downstream instead of every service failing at once.",
        fg="cyan",
    )
    for i, slug in enumerate(slugs, start=1):
        click.echo(f"  {i:>3}  {slug}")
    while True:
        raw = click.prompt(
            "  Gateway service — number or slug (blank = none)",
            default=current,
            show_default=bool(current),
        ).strip()
        if not raw:
            return ""
        if raw.isdigit() and 1 <= int(raw) <= len(slugs):
            return slugs[int(raw) - 1]
        if raw in slugs:
            return raw
        click.secho("    pick one from the list", fg="red")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@click.group("config")
def config_group() -> None:
    """Show .env, or change one section of it (after `init`)."""


def _require_env(project_dir: Path) -> Path:
    env_path = project_dir / ".env"
    if not env_path.is_file():
        raise click.ClickException(
            f"no .env at {env_path}. On a clean install, run `orchestrator-cli init`.\n"
            f"  The running orchestrator container has no .env — its settings arrive as\n"
            f"  environment variables — so run this through the setup service:\n"
            f"  docker compose run --rm setup config ..."
        )
    return env_path


@config_group.command("show")
@_DIR_OPTION
def config_show(project_dir: Path) -> None:
    """Print .env grouped by section. Secrets are masked, never shown."""
    try:
        values = read_env(_require_env(project_dir))
    except EnvFileError as exc:
        raise click.ClickException(str(exc)) from exc

    hidden = secret_names()
    for sec in ENV_SECTIONS:
        editable = f"   [config edit {sec.id}]" if sec.id in SECTIONS else ""
        click.secho(f"\n{sec.title}{editable}", bold=True)
        for name in sec.names():
            value = values.get(name, "")
            if not value:
                shown = "(unset)"
            elif name in hidden:
                shown = "****** (set)"
            else:
                shown = value
            click.echo(f"  {name:<30} {shown}")

    extra = [k for k in values if k not in set(layout_names())]
    if extra:
        click.secho("\nOther settings (not managed by the wizard)", bold=True)
        for name in extra:
            click.echo(f"  {name:<30} ****** (hidden: unknown keys may be secrets)")


@config_group.command("edit")
@click.argument("section_id", metavar="SECTION", type=click.Choice(list(SECTIONS)))
@_DIR_OPTION
@click.option("--no-verify", is_flag=True, help="Do not contact Proxmox or PBS.")
def config_edit(section_id: str, project_dir: Path, no_verify: bool) -> None:
    """Change one SECTION of .env. Everything else is kept exactly as it is."""
    env_path = _require_env(project_dir)
    yaml_path = project_dir / "config" / "services.yaml"
    try:
        previous = read_env(env_path)
        editor = RegistryEditor(yaml_path) if yaml_path.exists() else None
    except (EnvFileError, RegistryEditError) as exc:
        raise click.ClickException(str(exc)) from exc

    w = Wizard(
        _merge_previous(previous),
        interactive=True,
        verify=not no_verify,
        discovery=make_discovery(),
    )
    if editor is not None:
        w.always_on = [
            str(n.get("name")) for n in editor.doc.get("nodes") or [] if n.get("always_on")
        ]
        w.known_node_names = editor.node_names()
        w.power_targets = {
            str(n.get("name")): str(n.get("power_mgr_target"))
            for n in editor.doc.get("nodes") or []
            if n.get("power_mgr_target")
        }
        w.registry_slugs = editor.service_slugs()
    power_targets_before = dict(w.power_targets)

    click.secho("  Enter keeps the value in [brackets]; '-' clears an optional one.", fg="cyan")
    before = dict(w.values)
    SECTIONS[section_id](w)

    # Only what this section changed. Keys the old file lacked are filled from
    # defaults and written too, but they are not edits, so they are not listed.
    changed = [k for k in w.values if w.values[k] != before.get(k, "")]
    nodes_changed = False
    if section_id == "proxmox" and editor is not None:
        # Always-on is a property of the node in services.yaml, but it is answered
        # here, next to the host it is about.
        for name in editor.node_names():
            nodes_changed |= editor.ensure_node(name, always_on=name in w.always_on)
    changed_targets: dict[str, str] = {}
    if section_id == "power" and editor is not None:
        # Same idea: power_mgr_target lives in services.yaml, answered here next
        # to the manager it is about.
        for name, target in w.power_targets.items():
            if power_targets_before.get(name) != target:
                changed_targets[name] = target
            nodes_changed |= editor.ensure_node(name, power_mgr_target=target)

    if not changed and not nodes_changed:
        click.echo("\nNo changes.")
        return

    hidden = secret_names()
    click.secho("\nChanges:", bold=True)
    for key in changed:
        old, new = before.get(key, "") or "(unset)", w.values[key] or "(unset)"
        shown = "(changed)" if key in hidden else f"{old} -> {new}"
        click.echo(f"  {key:<30} {shown}")
    if section_id == "proxmox" and nodes_changed:
        click.echo(f"  always-on nodes in services.yaml -> {', '.join(w.always_on) or '(none)'}")
    for name, target in changed_targets.items():
        old = power_targets_before.get(name, name)
        click.echo(f"  {name} power_mgr_target in services.yaml -> {old} -> {target}")
    for problem in w.problems:
        click.secho(f"  - {problem}", fg="yellow")

    if not click.confirm("\n  Write?", default=True):
        click.echo("Nothing written.")
        return

    try:
        if changed:
            backup = write_env(env_path, w.values)
            click.secho(f"Wrote {env_path}", fg="green")
            if backup is not None:
                click.echo(f"  previous version kept as {backup}")
        if nodes_changed and editor is not None:
            editor.save()
            what = "power manager targets" if section_id == "power" else "always-on nodes"
            click.secho(f"Updated {what} in {yaml_path}", fg="green")
    except (EnvFileError, RegistryEditError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("\nApply it:  docker compose up -d   (recreates the container with the new .env)")


# ---------------------------------------------------------------------------
# scaffold — the update command
# ---------------------------------------------------------------------------


@click.command()
@click.option("--file", "path_override", default=None, help="services.yaml to update.")
@click.option("--node", default=None, help="Only consider guests on this node.")
@click.option("--include", default=None, help="New guests to add: slugs, VMIDs or 'all'.")
@click.option("--exclude", default=None, help="New guests to leave out.")
@click.option("--running-only/--all-guests", default=False, help="Only offer running guests.")
@click.option(
    "--policy",
    "policy_name",
    default=None,
    help="Backup policy for added services (default: the first in the file).",
)
@click.option("--with-probes", is_flag=True, help="Walk through a probe for each added service.")
@click.option("--yes", is_flag=True, help="Add the selection without confirming.")
@click.option(
    "--non-interactive", is_flag=True, help="Never prompt; adds only what --include names."
)
def scaffold(
    path_override: str | None,
    node: str | None,
    include: str | None,
    exclude: str | None,
    running_only: bool,
    policy_name: str | None,
    with_probes: bool,
    yes: bool,
    non_interactive: bool,
) -> None:
    """Pull cluster changes into services.yaml.

    The update command, for after `init`: adds new nodes, offers new guests, and
    follows guests that moved node. Existing services keep their name, probes and
    settings — guests are matched by VMID, so renaming a VM does not duplicate it.
    Guests that left the cluster are reported, never deleted.

    .env — the Proxmox connection, keys and host locations — is not touched.
    Change that with `config edit`.
    """
    set_non_interactive(non_interactive)
    interactive = interactive_default()
    path = registry_path(path_override)
    if not path.exists():
        raise click.ClickException(
            f"{path} does not exist. scaffold updates an existing registry; on a clean "
            f"install run `orchestrator-cli init`."
        )

    settings = get_settings()
    editor = open_editor(path_override)
    try:
        guests, cluster = asyncio.run(make_discovery().guests(settings))
    except AdapterError as exc:
        raise click.ClickException(f"could not read the cluster: {exc}") from exc

    if not guests:
        click.secho(
            "Proxmox returned no guests.\n"
            "  /cluster/resources filters by VM.Audit, so an empty list usually means the\n"
            "  token lacks it rather than that the cluster is empty. Check with\n"
            "  `pveum acl list` — you need rows for BOTH the user and the token.",
            fg="yellow",
        )
        raise SystemExit(1)

    all_guests = guests
    if node:
        guests = [g for g in guests if g.node == node]
    if running_only:
        guests = [g for g in guests if g.status == "running"]

    changes: list[str] = []
    known_nodes = set(editor.node_names())
    for cluster_node in cluster.nodes:
        if cluster_node.name not in known_nodes:
            editor.ensure_node(cluster_node.name, always_on=False)
            changes.append(
                f"node {cluster_node.name} added as powers-off "
                f"(`config edit proxmox` if it is always on)"
            )
    gone_nodes = sorted(known_nodes - {n.name for n in cluster.nodes}) if cluster.nodes else []

    candidates = build_candidates(guests, editor)
    for c in candidates:
        svc = editor.get_service(c.slug)
        if c.known and svc is not None and svc.get("node") != c.guest.node:
            editor.ensure_node(c.guest.node)
            changes.append(f"{c.slug}: moved {svc.get('node')} -> {c.guest.node}")
            editor.upsert_service(c.slug, {"node": c.guest.node})

    live = {(g.kind.value, g.vmid) for g in all_guests}
    stale = [
        str(svc.get("slug"))
        for svc in editor.doc.get("services") or []
        if svc.get("guest_id") is not None
        and (str(svc.get("guest_kind")), int(svc.get("guest_id"))) not in live
    ]

    gateway = settings.gateway_service if settings.gateway_service in editor.service_slugs() else ""
    new = [c for c in candidates if not c.known]
    chosen: list[Candidate] = []
    if new:
        click.secho(f"\n{len(new)} guest(s) not yet in {path}:", bold=True)
        chosen = choose_candidates(new, include, exclude, interactive=interactive, confirm=not yes)
    else:
        click.echo("\nNo new guests.")

    policy = policy_name or next(iter(editor.policy_names()), "daily-frequent")
    if chosen:
        editor.ensure_policy(policy, schedule_cron="0 3 * * *", retention={"keep_daily": 7})
    for c in chosen:
        editor.ensure_node(c.guest.node)
        editor.upsert_service(c.slug, new_service_fields(c, policy, gateway))

    if changes or chosen:
        save(editor, f"{len(chosen)} added, {len(changes)} other change(s)")
        for change in changes:
            click.echo(f"  {change}")
        if chosen:
            click.echo(f"  added: {', '.join(c.slug for c in chosen)}")
            if gateway:
                click.echo(f"  each depends on the gateway, {gateway}")
    else:
        click.secho(f"{path} is already up to date.", fg="green")

    if stale:
        click.secho(
            "\nIn services.yaml but no longer in the cluster (kept — remove with "
            "`service remove <slug>`):",
            fg="yellow",
        )
        click.echo("    " + ", ".join(stale))
    if gone_nodes:
        click.secho(f"\nNodes the cluster no longer reports: {', '.join(gone_nodes)}", fg="yellow")
    if settings.gateway_service and not gateway:
        click.secho(
            f"\nGATEWAY_SERVICE={settings.gateway_service!r} is not in {path}; new services "
            f"got no depends_on.",
            fg="yellow",
        )

    if with_probes:
        for c in chosen:
            click.secho(f"\n=== probes for {c.slug} ===", bold=True)
            while click.confirm(f"  Add a probe to {c.slug}?", default=True):
                kind = choose_probe_kind(None)
                entry = build_probe(kind, suggest=probe_suggestions(kind, editor, c.slug))
                editor.upsert_probe(c.slug, entry)
                save(editor, f"probe {entry['name']!r} added to {c.slug!r}")

    report_gate_readiness(editor)
