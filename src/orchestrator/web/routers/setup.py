"""First-run setup from the browser: the web form of ``orchestrator-cli init``.

Scope: the fields ``init --non-interactive`` reads (its ``--from`` env file
plus its flags), collected in ONE form — not the interactive wizard's
field-by-field validation-as-you-type, which does not map onto a web form.
Submitting runs ``cli_setup.plan_install`` / ``write_install``: the exact
code ``init`` runs, against the same live-cluster checks, writing .env and
config/services.yaml the same way (previous files backed up, atomic writes,
nothing written unless every check and both files validate).

Gating — when does ``/setup`` exist at all?

1. ``SETUP_PROJECT_DIR`` must be set. The orchestrator container normally
   gets .env as environment variables and has no writable copy of it (that
   is why compose has a separate ``setup`` service), so the page is off
   unless the operator deliberately mounts the project dir and points this
   at it.
2. That directory must not be configured yet — ``cli_setup.existing_config``,
   the very rule ``init`` uses to decide whether it would be replacing files.
   Once setup has written them, the page 404s for good; changing config later
   is ``config edit`` / ``scaffold``, not a re-run of a clean install.
3. Who may use it mirrors the ``/login`` bootstrap rule
   (``_admin_count(session) == 0``): while NO admin exists, anyone who can
   reach the page may — exactly as anyone may register the first admin. Once
   an admin exists, only a logged-in admin may.
"""

from __future__ import annotations

import io
import threading
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlmodel import Session

from orchestrator import cli_setup
from orchestrator.audit import log as audit
from orchestrator.config import Settings
from orchestrator.domain.enums import AuditResult
from orchestrator.registry.editor import RegistryEditError
from orchestrator.setup.envfile import EnvFileError, default_values, read_env
from orchestrator.web.auth import get_current_admin, login_redirect
from orchestrator.web.deps import SessionDep, SettingsDep
from orchestrator.web.routers.auth import _admin_count

router = APIRouter(tags=["setup"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

#: Actor recorded in the audit log when setup runs before any admin exists.
BOOTSTRAP_ACTOR = "setup-wizard"


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = "text"  # text | secret | bool | int | choice
    required: bool = False
    help: str = ""
    choices: tuple[str, ...] = ()


#: The .env keys init's sections read, grouped as init asks them. Only these
#: are accepted from the form — a POST cannot set DATABASE_URL, SETUP_PROJECT_DIR
#: or anything else init itself never asks for.
SECTIONS: tuple[tuple[str, str, tuple[Field, ...]], ...] = (
    (
        "Proxmox VE",
        "Point this at a node that is ALWAYS ON — if it powers off, the orchestrator "
        "loses the API exactly when it needs it to wake something.",
        (
            Field("PROXMOX_HOST", "Proxmox host (ip or name)", required=True),
            Field("PROXMOX_PORT", "API port", "int"),
            Field(
                "PROXMOX_VERIFY_TLS",
                "Verify its TLS certificate",
                "bool",
                help="Untick for Proxmox's default self-signed certificate.",
            ),
            Field(
                "PROXMOX_TOKEN_ID",
                "API token id",
                required=True,
                help="user@realm!tokenname — required privileges: docs/api_tokens.md",
            ),
            Field("PROXMOX_TOKEN_SECRET", "API token secret", "secret", required=True),
            Field("PROXMOX_RESTORE_TOKEN_ID", "Restore token id (blank = not yet)"),
            Field("PROXMOX_RESTORE_TOKEN_SECRET", "Restore token secret", "secret"),
        ),
    ),
    (
        "Proxmox Backup Server",
        "The orchestrator reads, verifies and prunes backups here.",
        (
            Field("PBS_HOST", "PBS host (ip or name)", required=True),
            Field("PBS_PORT", "API port", "int"),
            Field("PBS_VERIFY_TLS", "Verify its TLS certificate", "bool"),
            Field(
                "PBS_TOKEN_ID",
                "API token id",
                required=True,
                help="The ACL must exist for BOTH the user and the token.",
            ),
            Field("PBS_TOKEN_SECRET", "API token secret", "secret", required=True),
            Field(
                "PBS_DATASTORE",
                "Datastore",
                help="Blank = the only datastore PBS lists. Required if checks are off.",
            ),
            Field(
                "PBS_NODE_NAME",
                "PBS node name",
                help='"localhost" unless the PBS install was renamed.',
            ),
            Field(
                "PVE_BACKUP_STORAGE",
                "Proxmox storage ID for this datastore",
                help="Blank = worked out from Proxmox. Required if checks are off.",
            ),
            Field(
                "PBS_PROTECT_ENABLED",
                "Pin the last verified backup (needs Datastore.Modify)",
                "bool",
            ),
        ),
    ),
    (
        "Reaching your guests",
        "Probes usually reach guests through the gateway — docs/probing_through_a_gateway.md.",
        (
            Field(
                "PROBE_PROXY_BASE_URL",
                "Reverse proxy LOCAL address for HTTP probes",
                help="e.g. https://10.0.0.2 — blank = probes go direct.",
            ),
            Field(
                "HOST_RUNNER_SOCKET",
                "Host runner socket inside the container",
                help="Blank = not used. Usually /run/orchestrator/host-runner.sock.",
            ),
            Field(
                "SSH_KEY_PATH",
                "SSH private key path inside the container",
                help="Blank = no direct SSH from the container.",
            ),
            Field("SSH_KNOWN_HOSTS_PATH", "known_hosts path inside the container"),
            Field("SSH_VERIFY_HOST_KEY", "Verify SSH host keys", "bool"),
            Field("SSH_JUMP_HOST", "Default SSH jump host (blank = connect directly)"),
            Field("SSH_JUMP_USER", "Jump host user"),
            Field("SSH_JUMP_PORT", "Jump host SSH port", "int"),
        ),
    ),
    (
        "Power manager (Home Assistant MQTT)",
        "Blank host = skip for now; the power adapter stays a logged no-op.",
        (
            Field("MQTT_HOST", "MQTT broker host"),
            Field("MQTT_PORT", "Port", "int"),
            Field("MQTT_TLS", "Use TLS", "bool"),
            Field("MQTT_USERNAME", "Username"),
            Field("MQTT_PASSWORD", "Password", "secret"),
            Field("MQTT_POWER_TOPIC_PREFIX", "Runtime topic prefix (NOT the HA discovery prefix)"),
        ),
    ),
    (
        "Dashboard",
        "Passkeys are bound to the hostname the dashboard is served on. A new "
        "session secret is generated.",
        (
            Field("WEBAUTHN_RP_ID", "Hostname the dashboard will be served on", required=True),
            Field("WEBAUTHN_ORIGIN", "Full origin (scheme://host[:port])", required=True),
        ),
    ),
    (
        "Runtime",
        "",
        (
            Field(
                "ORCHESTRATOR_IMAGE",
                "Image to run",
                required=True,
                help="Pin a version tag rather than :latest — this is the control plane.",
            ),
            Field(
                "DRY_RUN",
                "Start in dry-run mode",
                "bool",
                help="Recommended until `orchestrator-cli check` passes.",
            ),
            Field(
                "LOG_LEVEL", "Log level", "choice", choices=("DEBUG", "INFO", "WARNING", "ERROR")
            ),
        ),
    ),
)

FIELDS: dict[str, Field] = {f.key: f for _, _, fields in SECTIONS for f in fields}
_SECRETS = {k for k, f in FIELDS.items() if f.kind == "secret"}

#: One setup at a time: the wizard prints through click to stdout, which is
#: captured process-wide for the duration — and two concurrent clean installs
#: into the same directory are nonsense anyway.
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# gating
# ---------------------------------------------------------------------------


def setup_dir(settings: Settings) -> Path | None:
    """The directory /setup may write, or None when the page must not exist."""
    project_dir = settings.setup_project_dir
    if project_dir is None or cli_setup.existing_config(project_dir):
        return None
    return project_dir


def _setup_dir_or_404(settings: Settings) -> Path:
    project_dir = setup_dir(settings)
    if project_dir is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    return project_dir


def _actor(request: Request, session: Session) -> str | None:
    """Who is running setup, or None if they may not (an admin exists and
    this visitor is not logged in as one)."""
    admin = get_current_admin(request, session)
    if admin is not None:
        return admin.username
    return BOOTSTRAP_ACTOR if _admin_count(session) == 0 else None


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------


@router.get("/setup", response_model=None)
async def setup_page(
    request: Request, session: SessionDep, settings: SettingsDep
) -> HTMLResponse | RedirectResponse:
    project_dir = _setup_dir_or_404(settings)
    actor = _actor(request, session)
    if actor is None:
        return login_redirect(request)

    values = default_values()
    # The browser is already talking to the dashboard's real hostname; offer it.
    host = request.url.hostname or "localhost"
    values["WEBAUTHN_RP_ID"] = host
    values["WEBAUTHN_ORIGIN"] = str(request.base_url).rstrip("/")
    env_path, yaml_path = cli_setup.install_paths(project_dir)
    return _templates.TemplateResponse(
        request,
        "setup.html",
        {
            "sections": SECTIONS,
            # Secrets are never echoed back into the page, even defaults.
            "values": {k: v for k, v in values.items() if k in FIELDS and k not in _SECRETS},
            "env_path": env_path,
            "yaml_path": yaml_path,
            "actor": actor,
        },
    )


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


class SetupRequest(BaseModel):
    """``values`` stands in for init's ``--from`` file; the rest are its flags."""

    values: dict[str, str] = {}
    always_on: str = ""
    include: str = "all"
    exclude: str = ""
    gateway: str = ""
    policy: str = "daily-frequent"
    verify: bool = True


def _error(status_code: int, detail: str, log: str = "") -> JSONResponse:
    return JSONResponse({"detail": detail, "log": log}, status_code=status_code)


@router.post("/api/setup", response_model=None)
def run_setup(
    body: SetupRequest, request: Request, session: SessionDep, settings: SettingsDep
) -> dict[str, Any] | JSONResponse:
    # A plain `def`, not `async def`: FastAPI runs it in a worker thread, where
    # the wizard's own asyncio.run() checks can start an event loop.
    project_dir = _setup_dir_or_404(settings)
    actor = _actor(request, session)
    if actor is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")

    unknown = sorted(set(body.values) - set(FIELDS))
    if unknown:
        return _error(400, f"not a setup field: {', '.join(unknown)}")
    missing = [
        f.label for f in FIELDS.values() if f.required and not body.values.get(f.key, "").strip()
    ]
    if missing:
        return _error(400, f"required: {', '.join(missing)}")

    if not _LOCK.acquire(blocking=False):
        return _error(409, "setup is already running")
    try:
        # Re-check under the lock: a concurrent request may just have written.
        if cli_setup.existing_config(project_dir):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
        return _install(session, project_dir, body, actor)
    finally:
        _LOCK.release()


def _install(
    session: Session, project_dir: Path, body: SetupRequest, actor: str
) -> dict[str, Any] | JSONResponse:
    env_path, yaml_path = cli_setup.install_paths(project_dir)
    output = io.StringIO()
    try:
        with redirect_stdout(output), redirect_stderr(output):
            previous = read_env(env_path) if env_path.is_file() else {}
            values = cli_setup._merge_previous(previous)
            # Blank means "keep the default", as Enter does in the CLI wizard.
            values.update({k: v.strip() for k, v in body.values.items() if v.strip()})
            w = cli_setup.Wizard(
                values,
                interactive=False,
                verify=body.verify,
                discovery=cli_setup.make_discovery(),
            )
            if body.always_on.strip():
                w.always_on = [p.strip() for p in body.always_on.split(",") if p.strip()]
            editor = cli_setup.plan_install(
                w,
                yaml_path,
                include=body.include.strip() or None,
                exclude=body.exclude.strip() or None,
                gateway=body.gateway.strip() or None,
                policy_name=body.policy.strip() or "daily-frequent",
            )
            backups = cli_setup.write_install(w, editor, env_path, yaml_path)
    except (click.ClickException, EnvFileError, RegistryEditError, OSError) as exc:
        detail = exc.format_message() if isinstance(exc, click.ClickException) else str(exc)
        audit.record(
            session,
            actor=actor,
            action="setup.failed",
            target=f"dir:{project_dir}",
            result=AuditResult.ERROR,
            details={"error": detail, "verified": body.verify},
        )
        session.commit()
        return _error(400, detail, output.getvalue())

    services = editor.service_slugs()
    audit.record(
        session,
        actor=actor,
        action="setup.completed",
        target=f"dir:{project_dir}",
        details={
            "services": len(services),
            "nodes": editor.node_names(),
            "verified": body.verify,
            "problems": w.problems,
        },
    )
    session.commit()
    return {
        "status": "written",
        "env_path": str(env_path),
        "yaml_path": str(yaml_path),
        "backups": [str(b) for b in backups],
        "services": services,
        "nodes": editor.node_names(),
        "problems": w.problems,
        "log": output.getvalue(),
        # The running process read its settings at startup; nothing above
        # changed them. The new .env applies when the container is recreated.
        "restart_required": True,
    }
