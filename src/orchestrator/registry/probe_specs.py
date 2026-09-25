"""Declarative field specs that drive the CLI's interactive probe builder.

One table, so teaching the CLI about a new probe kind is a data change rather
than new prompt code. Each entry says what to ask, how to parse the answer, and
what a sensible default looks like.

Kept out of :mod:`orchestrator.domain.schemas` on purpose: those models describe
what a probe config *is*, this describes how to *ask a human for one*. They
change for different reasons.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from orchestrator.domain.enums import ProbeKind


@dataclass(frozen=True)
class FieldSpec:
    key: str
    prompt: str
    default: Any = None
    required: bool = False
    #: Turns the typed string into the value stored in YAML.
    parse: Callable[[str], Any] | None = None
    help: str = ""
    #: Restrict to these values; the CLI shows them as choices.
    choices: tuple[str, ...] | None = None


def _csv_ints(raw: str) -> list[int]:
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def _csv_strs(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _bool(raw: str) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class ProbeKindSpec:
    """Everything the CLI needs to build one probe kind interactively."""

    summary: str
    fields: tuple[FieldSpec, ...] = ()
    #: Command probes need a transport block; network ones reach out themselves.
    needs_transport: bool = False
    default_timeout_s: int = 15
    #: Shown when the kind has a caveat worth knowing before choosing it.
    caution: str = ""
    suggested_name: str = "check"
    #: Merged into the transport block the builder writes. Lets a kind pin the
    #: parts that are not really the operator's choice (ansible_ping always uses
    #: the ping module) while keeping them visible in the YAML rather than
    #: hidden in code.
    transport_defaults: Mapping[str, Any] = field(default_factory=dict)
    #: Restrict the transport menu for kinds that only make sense one way.
    transport_choices: tuple[str, ...] | None = None


PROBE_SPECS: dict[ProbeKind, ProbeKindSpec] = {
    ProbeKind.HTTP: ProbeKindSpec(
        summary="HTTP request — the usual choice for anything with a web surface",
        suggested_name="http",
        default_timeout_s=10,
        fields=(
            FieldSpec(
                "url",
                "URL to request",
                required=True,
                help="e.g. https://service.lan/health",
            ),
            FieldSpec("method", "HTTP method", default="GET", choices=("GET", "HEAD")),
            FieldSpec(
                "expect_status",
                "Acceptable status codes (comma separated)",
                default=[200],
                parse=_csv_ints,
                help="401 is often correct for an API that requires auth",
            ),
            FieldSpec(
                "expect_body_contains",
                "Text the body must contain (blank to skip)",
                default=None,
            ),
            FieldSpec(
                "verify_tls",
                "Verify the TLS certificate?",
                default=True,
                parse=_bool,
                help="false for self-signed certs",
            ),
            FieldSpec(
                "host_header",
                "Host header to send (blank if the URL already names the host)",
                default=None,
                help=(
                    "Set this when the URL points at a reverse proxy's LOCAL ip: "
                    "the proxy routes on this name. Same idea as curl --resolve."
                ),
            ),
            FieldSpec(
                "sni_hostname",
                "TLS SNI hostname (blank = same as the Host header)",
                default=None,
                help="Only needed if SNI must differ from the Host header",
            ),
        ),
    ),
    ProbeKind.TCP: ProbeKindSpec(
        summary="TCP connect — for services with no HTTP surface (game servers, mail)",
        suggested_name="tcp",
        default_timeout_s=5,
        fields=(
            FieldSpec("host", "Host to connect to", required=True),
            FieldSpec("port", "Port", required=True, parse=int),
        ),
    ),
    ProbeKind.MQTT: ProbeKindSpec(
        summary="MQTT heartbeat — a message must arrive on a topic within a window",
        suggested_name="mqtt-heartbeat",
        default_timeout_s=40,
        caution=(
            "The broker lives inside the stack being managed, so a broker outage "
            "makes this UNKNOWN. Rarely a good sole required probe."
        ),
        fields=(
            FieldSpec("topic", "Topic to watch", required=True),
            FieldSpec("within_seconds", "Seconds to wait for a message", default=30, parse=int),
        ),
    ),
    ProbeKind.SYSTEMD: ProbeKindSpec(
        summary="systemctl is-active — the unit must be running",
        suggested_name="systemd",
        needs_transport=True,
        default_timeout_s=15,
        fields=(FieldSpec("unit", "Unit name", required=True, help="e.g. nginx.service"),),
    ),
    ProbeKind.DOCKER_PROJECT: ProbeKindSpec(
        summary="Compose project — every container running, none unhealthy",
        suggested_name="docker",
        needs_transport=True,
        default_timeout_s=30,
        fields=(
            FieldSpec(
                "project",
                "Compose project name",
                required=True,
                help="the com.docker.compose.project label value",
            ),
        ),
    ),
    ProbeKind.DB_MARIADB: ProbeKindSpec(
        summary="MariaDB integrity — mariadb-check plus a single-transaction dump",
        suggested_name="mariadb-integrity",
        needs_transport=True,
        default_timeout_s=600,
        caution="Slow on a real database. Credentials come from a defaults-file ON THE GUEST.",
        fields=(
            FieldSpec(
                "mysql_defaults_file",
                "Path to the defaults-extra-file on the guest",
                required=True,
                help="e.g. /root/.my-orchestrator.cnf — never put a password in this config",
            ),
            FieldSpec(
                "databases",
                "Databases to check (comma separated, blank = all)",
                default=[],
                parse=_csv_strs,
            ),
        ),
    ),
    ProbeKind.DB_REDIS: ProbeKindSpec(
        summary="Redis integrity — BGSAVE then redis-check-rdb on the fresh dump",
        suggested_name="redis-integrity",
        needs_transport=True,
        default_timeout_s=300,
        fields=(
            FieldSpec("dump_path", "RDB dump path", default="/var/lib/redis/dump.rdb"),
            FieldSpec("trigger_bgsave", "Trigger a fresh BGSAVE first?", default=True, parse=_bool),
        ),
    ),
    ProbeKind.DB_MONGO: ProbeKindSpec(
        summary="MongoDB integrity — mongodump exercises a full read of every collection",
        suggested_name="mongo-integrity",
        needs_transport=True,
        default_timeout_s=600,
        fields=(
            FieldSpec(
                "mongodump_uri",
                "mongodump URI",
                required=True,
                help="e.g. mongodb://user:pass@localhost:27017",
            ),
            FieldSpec("validate_db", "Single database to check (blank = all)", default=None),
        ),
    ),
    ProbeKind.DB_SQLITE: ProbeKindSpec(
        summary="SQLite integrity — PRAGMA integrity_check on a consistent snapshot",
        suggested_name="sqlite-integrity",
        needs_transport=True,
        default_timeout_s=120,
        fields=(FieldSpec("db_path", "Path to the database file on the guest", required=True),),
    ),
    ProbeKind.ANSIBLE_PLAYBOOK: ProbeKindSpec(
        summary="Ansible playbook — run by the host runner; exit 0 passes",
        suggested_name="playbook-check",
        needs_transport=True,
        transport_choices=("host_agent",),
        transport_defaults={"type": "host_agent", "action": "playbook"},
        default_timeout_s=600,
        caution=(
            "Give the playbook NAME, not a path — the runner resolves it against "
            "its own playbook_dir and must have it in allow.playbooks."
        ),
        fields=(
            FieldSpec(
                "playbook",
                "Playbook name as allowlisted on the runner",
                required=True,
                help="e.g. check-mariadb.yml — a name, never a path",
            ),
        ),
    ),
    ProbeKind.ANSIBLE_PING: ProbeKindSpec(
        summary="Ansible ping — the inventory host is reachable and answering",
        suggested_name="ansible-ping",
        needs_transport=True,
        default_timeout_s=60,
        transport_choices=("host_agent",),
        transport_defaults={"type": "host_agent", "action": "ping"},
        caution=(
            "Says the HOST is alive, not that the workload is healthy. A good "
            "liveness gate, a poor integrity one — pair it with a service check."
        ),
    ),
    ProbeKind.CUSTOM_SCRIPT: ProbeKindSpec(
        summary="Run a script on the guest — exit 0 passes",
        suggested_name="script",
        needs_transport=True,
        default_timeout_s=60,
        fields=(
            FieldSpec("executable", "Absolute path to the executable", required=True),
            FieldSpec(
                "args", "Arguments (comma separated, blank for none)", default=[], parse=_csv_strs
            ),
        ),
    ),
    ProbeKind.COMMAND: ProbeKindSpec(
        summary="Run an arbitrary command — exit 0 passes",
        suggested_name="command",
        needs_transport=True,
        default_timeout_s=60,
        caution="argv form, not a shell string. Use: sh, -lc, your command",
        fields=(
            FieldSpec(
                "argv",
                "Command as comma-separated argv",
                required=True,
                parse=_csv_strs,
                help="e.g.  sh,-lc,systemctl is-active foo",
            ),
        ),
    ),
}


TRANSPORT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "type",
        "Transport",
        default="host_agent",
        choices=("host_agent", "ssh", "local"),
        help=(
            "host_agent = ask the host runner (it owns the inventory and keys, "
            "so nothing sensitive enters this container — usually the right "
            "answer); ssh = direct from the container, needs a key mounted in; "
            "local = run inside the container itself"
        ),
    ),
    FieldSpec(
        "host",
        "Target host (inventory host/group for host_agent, hostname/ip for ssh)",
        required=True,
    ),
    FieldSpec("user", "SSH user (ignored by host_agent)", default="root"),
    FieldSpec("port", "SSH port (ignored by host_agent)", default=22, parse=int),
    FieldSpec(
        "jump_host",
        "Jump host (blank = use SSH_JUMP_HOST, '-' for a direct connection)",
        default=None,
        help=(
            "The orchestrator usually cannot reach guests directly and must hop "
            "through the gateway. Leave blank to use the configured default."
        ),
    ),
)


PROXY_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "hostname",
        "Public hostname",
        required=True,
        help="the name users browse to, e.g. media.example.com",
    ),
    FieldSpec(
        "upstream",
        "Upstream the proxy forwards to",
        required=True,
        help="e.g. http://10.0.0.10:8096",
    ),
    FieldSpec(
        "router_provider",
        "Which proxy serves it",
        default="traefik",
        choices=("traefik", "pangolin", "npm", "other"),
    ),
)


def kinds_by_menu_order() -> list[ProbeKind]:
    """Network probes first — they need no SSH and are what most services want."""
    order = [
        ProbeKind.HTTP,
        ProbeKind.TCP,
        ProbeKind.MQTT,
        ProbeKind.SYSTEMD,
        ProbeKind.DOCKER_PROJECT,
        ProbeKind.DB_MARIADB,
        ProbeKind.DB_REDIS,
        ProbeKind.DB_MONGO,
        ProbeKind.DB_SQLITE,
        ProbeKind.ANSIBLE_PING,
        ProbeKind.ANSIBLE_PLAYBOOK,
        ProbeKind.CUSTOM_SCRIPT,
        ProbeKind.COMMAND,
    ]
    # Anything added to the enum but not listed here still shows up, at the end.
    return order + [k for k in PROBE_SPECS if k not in order]
