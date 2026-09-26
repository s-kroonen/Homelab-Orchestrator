"""The ``.env`` file: its layout, and reading and writing it safely.

One declared layout drives everything that touches the file, so none of it can
drift:

* ``orchestrator-cli init`` and ``config edit`` write ``.env`` from it;
* ``.env.example`` is rendered from it — a test fails if the committed copy
  differs, so a new setting cannot land undocumented;
* ``config show`` groups values by it and masks the secrets.

Writing is deliberately careful. ``.env`` holds every credential the
orchestrator has and is git-ignored, so git cannot bring it back: the previous
file is always copied aside before it is replaced, and the replacement itself is
atomic.

Regenerate the example after changing the layout::

    python -m orchestrator.setup.envfile
"""

from __future__ import annotations

import contextlib
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import SecretStr

from orchestrator.config import Settings


class EnvFileError(Exception):
    """A value cannot be represented in the file, or the file cannot be read."""


@dataclass(frozen=True)
class EnvKey:
    name: str
    comment: str = ""
    secret: bool = False


@dataclass(frozen=True)
class EnvSection:
    id: str
    title: str
    keys: tuple[EnvKey, ...]

    def names(self) -> list[str]:
        return [k.name for k in self.keys]


ENV_SECTIONS: tuple[EnvSection, ...] = (
    EnvSection(
        "image",
        "Image (read by docker-compose.yml)",
        (
            EnvKey(
                "ORCHESTRATOR_IMAGE",
                "Pin a specific tag — never rely on :latest for the control plane.\n"
                "Tags: https://github.com/s-kroonen/homelab-orchestrator/pkgs/container/"
                "homelab-orchestrator",
            ),
        ),
    ),
    EnvSection(
        "runtime",
        "Runtime",
        (
            EnvKey("ORCHESTRATOR_ENV", "development | production"),
            EnvKey("LOG_LEVEL", "DEBUG | INFO | WARNING | ERROR"),
            EnvKey("LOG_FORMAT", "json | console"),
            EnvKey(
                "DRY_RUN",
                "Swaps ALL external adapters for logging no-ops: nothing is started,\n"
                "stopped or backed up. Leave on until `orchestrator-cli check` passes.",
            ),
            EnvKey("POWER_ADAPTER", "mqtt | dry_run — every adapter is dry_run while DRY_RUN=true"),
            EnvKey("PROXMOX_ADAPTER", "api | dry_run"),
            EnvKey("PBS_ADAPTER", "api | dry_run"),
            EnvKey("NOTIFIER_ADAPTER", "null — the only notifier until phase 9"),
        ),
    ),
    EnvSection(
        "web",
        "Web / API",
        (
            EnvKey("HTTP_HOST"),
            EnvKey("HTTP_PORT"),
            EnvKey(
                "WEBAUTHN_RP_ID",
                "MUST match the hostname the dashboard is served on. Passkeys only work\n"
                "over HTTPS or on localhost.",
            ),
            EnvKey("WEBAUTHN_RP_NAME"),
            EnvKey("WEBAUTHN_ORIGIN"),
            EnvKey(
                "SESSION_SECRET",
                "Cookie signing secret. `init` generates one. By hand:\n"
                'python -c "import secrets;print(secrets.token_urlsafe(64))"',
                secret=True,
            ),
        ),
    ),
    EnvSection(
        "persistence",
        "Persistence and registry",
        (
            EnvKey("DATABASE_URL", "SQLite lives on a local Docker volume — NOT on NFS (locking)."),
            EnvKey("STATE_DIR"),
            EnvKey("NFS_STATE_DUMP_PATH", "Optional periodic dump of SQLite to durable storage."),
            EnvKey("STATE_DUMP_INTERVAL_MINUTES"),
            EnvKey("RUN_MIGRATIONS_ON_START", "Run alembic upgrade head on container start."),
            EnvKey(
                "SERVICES_YAML_PATH",
                "The SAVED registry. Boot reconciles it into the DB; the dashboard edits\n"
                "the DB live and can Save back here or Reset from here.",
            ),
        ),
    ),
    EnvSection(
        "proxmox",
        "Proxmox VE",
        (
            EnvKey(
                "PROXMOX_HOST",
                "Point this at an ALWAYS-ON node. Proxmox proxies API calls across the\n"
                "cluster, so one endpoint reaches every node — but if this one powers off,\n"
                "the orchestrator loses the API exactly when it needs it to wake something.\n"
                "See docs/proxmox_connectivity.md.",
            ),
            EnvKey("PROXMOX_PORT"),
            EnvKey("PROXMOX_VERIFY_TLS", "false for Proxmox's default self-signed certificate."),
            EnvKey(
                "PROXMOX_TOKEN_ID",
                "Routine token: backups and guest power. Privileges: docs/api_tokens.md",
            ),
            EnvKey("PROXMOX_TOKEN_SECRET", secret=True),
            EnvKey(
                "PROXMOX_RESTORE_TOKEN_ID",
                "Higher-privilege token, used only inside the restore flow (phase 8).",
            ),
            EnvKey("PROXMOX_RESTORE_TOKEN_SECRET", secret=True),
            EnvKey(
                "PVE_BACKUP_STORAGE",
                "The PVE *storage ID* pointing at PBS — what `vzdump storage=` expects.\n"
                "Find it with `pvesm status`. NOT necessarily the same as PBS_DATASTORE.",
            ),
        ),
    ),
    EnvSection(
        "pbs",
        "Proxmox Backup Server",
        (
            EnvKey("PBS_HOST"),
            EnvKey("PBS_PORT"),
            EnvKey("PBS_VERIFY_TLS"),
            EnvKey("PBS_DATASTORE", "The PBS *datastore name* — what the PBS API addresses."),
            EnvKey(
                "PBS_NODE_NAME",
                'Node name PBS runs tasks under; "localhost" on a standalone install.',
            ),
            EnvKey(
                "PBS_TOKEN_ID",
                "The ACL must exist for BOTH the user and the token: a token's privileges\n"
                "are the intersection of the two.",
            ),
            EnvKey("PBS_TOKEN_SECRET", secret=True),
            EnvKey(
                "PBS_PROTECT_ENABLED",
                "Pinning the known-good snapshot needs Datastore.Modify. Set false if the\n"
                "token lacks it: protect calls become logged no-ops instead of 403s.\n"
                "Trade-off: nothing then stops prune removing the last verified backup.",
            ),
        ),
    ),
    EnvSection(
        "network",
        "Reaching guests (probes)",
        (
            EnvKey(
                "PROBE_PROXY_BASE_URL",
                "The reverse proxy's LOCAL address, e.g. https://10.0.0.2. `probe add` points\n"
                "HTTP probes here and routes with a Host header (like curl --resolve). It is\n"
                "written into each probe explicitly, never applied behind your back.",
            ),
            EnvKey(
                "GATEWAY_SERVICE",
                "Slug of the gateway service. New services get depends_on: [it], so a gateway\n"
                "outage reads as UNKNOWN downstream instead of every service failing at once.",
            ),
            EnvKey(
                "HOST_RUNNER_SOCKET",
                "RECOMMENDED for command probes. The runner executes checks on the HOST; this\n"
                "container gets only the socket — no inventory, playbooks or keys.\n"
                "See docs/host_runner.md.",
            ),
            EnvKey(
                "SSH_JUMP_HOST",
                "Default ProxyJump for direct SSH probes. A probe can override it, or set\n"
                'jump_host: "" to connect directly. See docs/probing_through_a_gateway.md.',
            ),
            EnvKey("SSH_JUMP_USER"),
            EnvKey("SSH_JUMP_PORT"),
            EnvKey(
                "SSH_KEY_PATH",
                "Only for direct SSH probes. The key must be MOUNTED into the container, which\n"
                "puts it inside the web listener's blast radius — prefer the host runner.",
            ),
            EnvKey("SSH_KNOWN_HOSTS_PATH"),
            EnvKey("SSH_VERIFY_HOST_KEY"),
        ),
    ),
    EnvSection(
        "timeouts",
        "Task timeouts",
        (
            EnvKey("BACKUP_TASK_TIMEOUT_S", "A large VM dump can legitimately take hours."),
            EnvKey("VERIFY_TASK_TIMEOUT_S"),
            EnvKey(
                "WAKE_TIMEOUT_S",
                "Bounds the whole wake pipeline — a cold node can take minutes to POST, "
                "boot, and bring its guests up.",
            ),
            EnvKey("WAKE_POLL_INTERVAL_S"),
            EnvKey("WAKE_GUEST_START_TIMEOUT_S", "Just the Proxmox start task, not guest boot."),
            EnvKey(
                "WAKE_RETRY_COOLDOWN_S",
                "After a wake ends, the next hit reuses it rather than retrying — without "
                "this, a fast failure gets re-triggered by every visitor request during "
                "an outage, not just page reloads.",
            ),
        ),
    ),
    EnvSection(
        "power",
        "Power manager (Home Assistant MQTT)",
        (
            EnvKey(
                "MQTT_HOST",
                "Stored for phase 3 (wake). Until then the power adapter is a logged no-op.",
            ),
            EnvKey("MQTT_PORT"),
            EnvKey("MQTT_TLS"),
            EnvKey("MQTT_USERNAME"),
            EnvKey("MQTT_PASSWORD", secret=True),
            EnvKey(
                "MQTT_POWER_TOPIC_PREFIX",
                "The manager's RUNTIME prefix, e.g. ipmi-manager — commands publish to\n"
                "<prefix>/<target>/command, state/availability are <prefix>/<target>/state\n"
                "and .../availability. This is NOT the Home Assistant discovery prefix\n"
                '("homeassistant/ipmi-manager/..." only carries entity-config payloads).',
            ),
        ),
    ),
    EnvSection(
        "notifier",
        "Notifier (phase 9)",
        (
            EnvKey("MAIL_SMTP_HOST", "mailcow SMTP — used only while mailcow itself is healthy."),
            EnvKey("MAIL_SMTP_PORT"),
            EnvKey("MAIL_SMTP_USERNAME"),
            EnvKey("MAIL_SMTP_PASSWORD", secret=True),
            EnvKey("MAIL_FROM"),
            EnvKey("NTFY_URL", "Offsite ntfy for instant fallback alerts."),
            EnvKey("NTFY_TOKEN", secret=True),
        ),
    ),
)

#: Values describing the Docker deployment rather than the app's own defaults.
#: The app defaults to local paths so it runs natively; docker-compose.yml mounts
#: /data and /etc/orchestrator, so that is what a deployment .env says.
DEPLOY_DEFAULTS: dict[str, str] = {
    "ORCHESTRATOR_IMAGE": "ghcr.io/s-kroonen/homelab-orchestrator:latest",
    "ORCHESTRATOR_ENV": "production",
    "LOG_FORMAT": "json",
    "DRY_RUN": "true",
    "POWER_ADAPTER": "mqtt",
    "PROXMOX_ADAPTER": "api",
    "PBS_ADAPTER": "api",
    "NOTIFIER_ADAPTER": "null",
    "DATABASE_URL": "sqlite:////data/orchestrator.db",
    "STATE_DIR": "/data",
    "SERVICES_YAML_PATH": "/etc/orchestrator/services.yaml",
    "RUN_MIGRATIONS_ON_START": "true",
    # Empty, not the app's dev placeholder: `init` generates a real one.
    "SESSION_SECRET": "",
    # The app's defaults for these are example.lan placeholders. Offered as a
    # wizard default, a placeholder is one Enter away from being written.
    "PROXMOX_HOST": "",
    "PVE_BACKUP_STORAGE": "",
    "PBS_HOST": "",
    "PBS_DATASTORE": "",
    "MQTT_HOST": "",
}

#: Placeholders for .env.example. Obviously fake — this repository is public.
EXAMPLE_VALUES: dict[str, str] = {
    "ORCHESTRATOR_IMAGE": "ghcr.io/s-kroonen/homelab-orchestrator:1.0.0",
    "WEBAUTHN_RP_ID": "orchestrator.example.lan",
    "WEBAUTHN_ORIGIN": "https://orchestrator.example.lan",
    "SESSION_SECRET": "change-me-generate-a-long-random-string",
    "PROXMOX_HOST": "proxmox.example.lan",
    "PROXMOX_TOKEN_ID": "orchestrator@pve!backups",
    "PROXMOX_TOKEN_SECRET": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "PROXMOX_RESTORE_TOKEN_ID": "orchestrator@pve!restore",
    "PROXMOX_RESTORE_TOKEN_SECRET": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "PVE_BACKUP_STORAGE": "example-pbs-storage",
    "PBS_HOST": "pbs.example.lan",
    "PBS_DATASTORE": "example-datastore",
    "PBS_TOKEN_ID": "orchestrator@pbs!datastore",
    "PBS_TOKEN_SECRET": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "MQTT_HOST": "mqtt.example.lan",
    "MQTT_USERNAME": "orchestrator",
    "MQTT_PASSWORD": "change-me",
    "MAIL_SMTP_HOST": "mail.example.lan",
    "MAIL_SMTP_USERNAME": "orchestrator@example.lan",
    "MAIL_SMTP_PASSWORD": "change-me",
    "MAIL_FROM": "orchestrator@example.lan",
    "NTFY_URL": "https://ntfy.example.com/homelab-alerts",
}

EXAMPLE_HEADER = """homelab-orchestrator — example environment file

You should not need to copy this. The setup wizard writes .env for you and
checks every value against your cluster as it goes:

    docker compose run --rm setup init

By hand: copy to `.env` (git-ignored) and fill in. Never commit real hostnames,
IPs, tokens or keys to a public repository.

Generated from src/orchestrator/setup/envfile.py — edit the layout there, then
run `python -m orchestrator.setup.envfile` to regenerate this file."""

GENERATED_HEADER = """homelab-orchestrator — environment file

Written by `orchestrator-cli init` on {date}. It holds credentials: git-ignored,
never commit it.

Change one part:  orchestrator-cli config edit <section>
Sections:         {sections}
Keys you add by hand are kept when a section is rewritten."""

_RULE = "# " + "=" * 78


# ---------------------------------------------------------------------------
# layout helpers
# ---------------------------------------------------------------------------


def section(section_id: str) -> EnvSection:
    for sec in ENV_SECTIONS:
        if sec.id == section_id:
            return sec
    raise KeyError(section_id)


def secret_names() -> set[str]:
    return {k.name for sec in ENV_SECTIONS for k in sec.keys if k.secret}


def layout_names() -> list[str]:
    return [k.name for sec in ENV_SECTIONS for k in sec.keys]


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def default_values() -> dict[str, str]:
    """What a fresh deployment ``.env`` contains before anyone answers a question."""
    out = {name.upper(): _stringify(f.default) for name, f in Settings.model_fields.items()}
    out.update(DEPLOY_DEFAULTS)
    return {name: out.get(name, "") for name in layout_names()}


# ---------------------------------------------------------------------------
# format / render / read / write
# ---------------------------------------------------------------------------

# Characters that need quoting to survive BOTH readers of this file: docker
# compose (compose-go) and python-dotenv (via pydantic-settings). Single quotes
# are literal in both, which is why they are the only quoting used.
_QUOTE_TRIGGERS = frozenset(' \t#$\\"')


def format_value(name: str, value: str) -> str:
    if "\n" in value or "\r" in value:
        raise EnvFileError(f"{name}: a value cannot contain a line break")
    if not value:
        return ""
    if not (_QUOTE_TRIGGERS.intersection(value) or value[0] == "'"):
        return value

    # Two readers with different rules, and no quoting both agree on for these:
    # python-dotenv unescapes \\ and \' inside single quotes and expands ${VAR} even
    # there; compose reads single-quoted values literally. Refuse rather than write
    # a value the app and compose would each see differently.
    if "'" in value or "${" in value or "\\\\" in value or value.endswith("\\"):
        raise EnvFileError(
            f"{name}: this value contains a quote, '${{' or backslashes in a way docker "
            f"compose and python-dotenv would read differently. Choose a different value "
            f"(e.g. a password without ', ${{ or \\)."
        )
    return f"'{value}'"


def _comment(text: str) -> list[str]:
    return [f"# {line}" if line else "#" for line in text.splitlines()]


def _section_rule(title: str) -> str:
    return f"# ---- {title} " + "-" * max(4, 72 - len(title))


def render_env(values: dict[str, str], header: str) -> str:
    """Render the whole file. Raises before returning if any value is unwritable."""
    lines = [_RULE, *_comment(header), _RULE]
    known: set[str] = set()

    for sec in ENV_SECTIONS:
        lines += ["", _section_rule(sec.title)]
        for i, key in enumerate(sec.keys):
            known.add(key.name)
            if key.comment:
                if i:
                    lines.append("")
                lines += _comment(key.comment)
            lines.append(f"{key.name}={format_value(key.name, values.get(key.name, ''))}")

    # Never drop what the operator added by hand — an unknown key may well be read
    # by docker-compose.yml, or by a future version of the app.
    extra = [k for k in values if k not in known]
    if extra:
        lines += ["", _section_rule("Other settings (kept from your previous file)")]
        lines += [f"{k}={format_value(k, values[k])}" for k in extra]

    return "\n".join(lines) + "\n"


def render_example() -> str:
    return render_env({**default_values(), **EXAMPLE_VALUES}, EXAMPLE_HEADER)


def render_generated(values: dict[str, str]) -> str:
    header = GENERATED_HEADER.format(
        date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        sections=", ".join(sec.id for sec in ENV_SECTIONS),
    )
    return render_env(values, header)


def read_env(path: Path) -> dict[str, str]:
    """Values as the app will see them. A missing file reads as empty."""
    if path.is_dir():
        raise EnvFileError(
            f"{path} is a directory, not a file (usually a Docker bind mount of a file "
            f"that did not exist)"
        )
    if not path.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(path, encoding="utf-8").items()}


def backup_file(path: Path) -> Path | None:
    """Copy ``path`` aside as ``<stem>.bak-<timestamp><suffix>``; return the copy.

    The name matches the ``*.bak-*`` rule in .gitignore, so a backup holding
    credentials or addresses cannot be committed by accident.
    """
    if not path.exists() or path.stat().st_size == 0:
        return None  # nothing worth keeping — an empty .env is what `touch .env` leaves
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.stem}.bak-{stamp}{path.suffix}")
    n = 1
    while dest.exists():
        dest = path.with_name(f"{path.stem}.bak-{stamp}-{n}{path.suffix}")
        n += 1
    shutil.copy2(path, dest)
    return dest


def write_text_atomic(path: Path, text: str, *, default_mode: int = 0o600) -> None:
    """Replace ``path`` in one step, keeping the old file's permissions if it had one."""
    mode = (path.stat().st_mode & 0o777) if path.exists() else default_mode
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    # Windows only honours the read-only bit; there is nothing more useful to do.
    with contextlib.suppress(OSError):
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_env(path: Path, values: dict[str, str]) -> Path | None:
    """Back up the current file, then write the new one. Returns the backup path."""
    text = render_generated(values)  # render first: a bad value fails before anything moves
    backup = backup_file(path)
    write_text_atomic(path, text)
    return backup


if __name__ == "__main__":
    target = Path(".env.example")
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_example())
    print(f"wrote {target}")
