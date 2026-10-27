"""The kinds of connection the orchestrator knows, and the fields each one has.

A *connection* is one instance of a thing the orchestrator talks to: a Proxmox cluster, a PBS, a
Traefik Manager, an MQTT broker, a Portainer, a Home Assistant, a notification channel, the SSH
settings. They used to be ``.env`` keys, one of each; they now live in ``connections.yaml``
(edited from the dashboard, applied without a restart) and can exist several times.

Every field names the legacy :class:`~orchestrator.config.Settings` attribute it fills for the
connection that is *in use* (the default / first enabled one of its kind), so code written against
``settings.portainer_url`` keeps working while the data moves. Defaults, types and secrecy come
from that attribute: this table cannot drift from ``Settings``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr

from orchestrator.config import Settings


@dataclass(frozen=True)
class FieldSpec:
    name: str
    setting: str
    label: str
    required: bool = False
    help: str = ""
    choices: tuple[str, ...] = ()
    #: Show as a password box even when the Settings field is a plain string.
    secret: bool = False

    @property
    def setting_default(self) -> Any:
        """What ``Settings`` itself starts with (placeholders and all)."""
        default = Settings.model_fields[self.setting].default
        return default.get_secret_value() if isinstance(default, SecretStr) else default

    @property
    def default(self) -> Any:
        """The value of a field nobody filled in. ``Settings`` ships placeholders such as
        ``proxmox.example.lan``; a connection never inherits those."""
        value = self.setting_default
        if isinstance(value, str) and "example" in value.lower():
            return ""
        return value

    @property
    def is_secret(self) -> bool:
        return self.secret or isinstance(Settings.model_fields[self.setting].default, SecretStr)

    @property
    def type(self) -> str:
        if self.is_secret:
            return "secret"
        if self.choices:
            return "choice"
        default = self.default
        if isinstance(default, bool):
            return "bool"
        if isinstance(default, int):
            return "int"
        if isinstance(default, float):
            return "float"
        return "text"


@dataclass(frozen=True)
class KindSpec:
    kind: str
    title: str
    blurb: str
    fields: tuple[FieldSpec, ...]
    #: The field whose value says "this is set up" (blank = not configured).
    marker: str
    #: Settings attribute that picks live vs dry-run for this kind, if it has one, and the value
    #: that means live. Having an enabled connection is what chooses live now.
    selector: tuple[str, str] | None = None
    #: May several be enabled at once? Today only Portainer can; the rest are one active
    #: connection each (the model allows more - the multi-network step switches them on).
    multi: bool = False
    #: A "notifier channel" kind: it is part of the combined notifier, not an adapter of its own.
    channel: bool = False

    def field(self, name: str) -> FieldSpec:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)


def _f(name: str, setting: str, label: str, **kw: Any) -> FieldSpec:
    return FieldSpec(name, setting, label, **kw)


KINDS: tuple[KindSpec, ...] = (
    KindSpec(
        "proxmox",
        "Proxmox VE",
        "Point this at a node that is ALWAYS ON - if it powers off, the orchestrator loses the API "
        "exactly when it needs it to wake something.",
        (
            _f("host", "proxmox_host", "Host (ip or name)", required=True),
            _f("port", "proxmox_port", "API port"),
            _f("verify_tls", "proxmox_verify_tls", "Verify its TLS certificate"),
            _f(
                "token_id",
                "proxmox_token_id",
                "API token id",
                required=True,
                help="user@realm!tokenname - privileges: docs/api_tokens.md",
            ),
            _f("token_secret", "proxmox_token_secret", "API token secret", required=True),
            _f("restore_token_id", "proxmox_restore_token_id", "Restore token id (optional)"),
            _f("restore_token_secret", "proxmox_restore_token_secret", "Restore token secret"),
            _f(
                "backup_storage",
                "pve_backup_storage",
                "Storage id of the PBS datastore",
                help="Blank = worked out from Proxmox.",
            ),
        ),
        marker="host",
        selector=("proxmox_adapter", "live"),
    ),
    KindSpec(
        "pbs",
        "Proxmox Backup Server",
        "The orchestrator reads, verifies and prunes backups here.",
        (
            _f("host", "pbs_host", "Host (ip or name)", required=True),
            _f("port", "pbs_port", "API port"),
            _f("verify_tls", "pbs_verify_tls", "Verify its TLS certificate"),
            _f("token_id", "pbs_token_id", "API token id", required=True),
            _f("token_secret", "pbs_token_secret", "API token secret", required=True),
            _f("datastore", "pbs_datastore", "Datastore", help="Blank = the only one PBS lists."),
            _f("node_name", "pbs_node_name", "PBS node name", help='"localhost" unless renamed.'),
            _f("protect_enabled", "pbs_protect_enabled", "Pin the last verified backup"),
        ),
        marker="host",
        selector=("pbs_adapter", "live"),
    ),
    KindSpec(
        "proxy",
        "Traefik Manager",
        "Where proxy hosts are written. The probe address and the defaults for new routes live here too.",
        (
            _f("url", "traefik_manager_url", "URL", required=True),
            _f("api_key", "traefik_manager_api_key", "API key", required=True),
            _f("verify_tls", "traefik_manager_verify_tls", "Verify its TLS certificate"),
            _f("timeout_s", "traefik_manager_timeout_s", "Timeout (seconds)"),
            _f(
                "default_entrypoints",
                "traefik_default_entrypoints",
                "Default entry points for a new host",
                help="Comma list; blank = websecure if it exists.",
            ),
            _f(
                "default_cert_resolver",
                "traefik_default_cert_resolver",
                "Default certificate resolver",
            ),
            _f("redirect_middleware", "traefik_redirect_middleware", "HTTP -> HTTPS middleware"),
            _f("hsts_middleware", "traefik_hsts_middleware", "HSTS headers middleware"),
            _f(
                "config_file",
                "traefik_manager_config_file",
                "Config file for a new route",
                help="{service} and {hostname} are filled in.",
            ),
            _f(
                "probe_base_url",
                "probe_proxy_base_url",
                "Reverse proxy LOCAL address for HTTP probes",
                help="e.g. https://10.0.0.2 - blank = probes go direct.",
            ),
        ),
        marker="url",
        selector=("proxy_adapter", "traefik_manager"),
    ),
    KindSpec(
        "traefik_api",
        "Traefik API (traffic metrics)",
        "Traefik's own read-only API, for traffic numbers and drift checks.",
        (
            _f("url", "traefik_api_url", "URL", required=True),
            _f("verify_tls", "traefik_api_verify_tls", "Verify its TLS certificate"),
            _f("username", "traefik_api_username", "Basic-auth user (optional)"),
            _f("password", "traefik_api_password", "Basic-auth password"),
            _f("timeout_s", "traefik_api_timeout_s", "Timeout (seconds)"),
        ),
        marker="url",
    ),
    KindSpec(
        "power",
        "Power manager (MQTT)",
        "Wakes and powers off nodes through Home Assistant's MQTT.",
        (
            _f("host", "mqtt_host", "Broker host", required=True),
            _f("port", "mqtt_port", "Port"),
            _f("tls", "mqtt_tls", "Use TLS"),
            _f("username", "mqtt_username", "Username"),
            _f("password", "mqtt_password", "Password"),
            _f(
                "topic_prefix",
                "mqtt_power_topic_prefix",
                "Runtime topic prefix",
                help="NOT the Home Assistant discovery prefix.",
            ),
        ),
        marker="host",
        selector=("power_adapter", "mqtt"),
    ),
    KindSpec(
        "portainer",
        "Portainer",
        "Stacks and containers. Add one per Portainer; a service's controller names which.",
        (
            _f("url", "portainer_url", "URL (how the orchestrator reaches it)", required=True),
            _f("api_key", "portainer_api_key", "Access token", required=True),
            _f("verify_tls", "portainer_verify_tls", "Verify its TLS certificate"),
            _f(
                "read_only",
                "portainer_read_only",
                "Read only",
                help="Nothing is started, stopped, redeployed, created or deleted through it.",
            ),
            _f(
                "public_url",
                "portainer_public_url",
                "Address people open in a browser",
                help='For "Open in Portainer" links; blank = the URL.',
            ),
        ),
        marker="url",
        selector=("portainer_adapter", "api"),
        multi=True,
    ),
    KindSpec(
        "haos",
        "Home Assistant (add-ons)",
        "For controllers that start and stop Home Assistant add-ons.",
        (
            _f("url", "haos_url", "URL", required=True),
            _f("token", "haos_token", "Long-lived access token", required=True),
            _f("verify_tls", "haos_verify_tls", "Verify its TLS certificate"),
        ),
        marker="url",
    ),
    KindSpec(
        "ntfy",
        "ntfy",
        "Alerts to an ntfy topic.",
        (
            _f("url", "ntfy_url", "Topic URL", required=True),
            _f("token", "ntfy_token", "Access token (optional)"),
        ),
        marker="url",
        channel=True,
    ),
    KindSpec(
        "email",
        "E-mail (SMTP)",
        "Alerts by mail.",
        (
            _f("host", "mail_smtp_host", "SMTP host", required=True),
            _f("port", "mail_smtp_port", "Port"),
            _f("starttls", "mail_smtp_starttls", "Use STARTTLS"),
            _f("username", "mail_smtp_username", "Username"),
            _f("password", "mail_smtp_password", "Password"),
            _f("sender", "mail_from", "From address"),
            _f("to", "mail_to", "To (comma list)", required=True),
        ),
        marker="host",
        channel=True,
    ),
    KindSpec(
        "webhook",
        "Webhook",
        "Alerts as JSON to a URL.",
        (
            _f("url", "notify_webhook_url", "URL", required=True),
            _f("token", "notify_webhook_token", "Bearer token (optional)"),
        ),
        marker="url",
        channel=True,
    ),
    KindSpec(
        "ssh",
        "SSH and host runner",
        "How checks and controllers reach guests that are not on this network.",
        (
            _f("key_path", "ssh_key_path", "Private key path inside the container"),
            _f("known_hosts_path", "ssh_known_hosts_path", "known_hosts path"),
            _f("verify_host_key", "ssh_verify_host_key", "Verify host keys"),
            _f("jump_host", "ssh_jump_host", "Default jump host"),
            _f("jump_user", "ssh_jump_user", "Jump host user"),
            _f("jump_port", "ssh_jump_port", "Jump host port"),
            _f("host_runner_socket", "host_runner_socket", "Host runner socket"),
        ),
        marker="key_path",
    ),
)

BY_KIND: dict[str, KindSpec] = {k.kind: k for k in KINDS}


def kind_spec(kind: str) -> KindSpec:
    try:
        return BY_KIND[kind]
    except KeyError:
        raise ValueError(
            f"unknown connection kind {kind!r} (one of: {', '.join(BY_KIND)})"
        ) from None


#: Every Settings attribute a connection fills - those are no longer ``.env`` business.
MANAGED_SETTINGS: frozenset[str] = frozenset(f.setting for k in KINDS for f in k.fields) | {
    sel[0] for k in KINDS if (sel := k.selector) is not None
}
