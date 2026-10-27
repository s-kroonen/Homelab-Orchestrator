"""Is this connection usable? One read-only look per kind, run from the dashboard before or after
saving. Nothing here changes anything on the other side; in dry-run mode nothing is contacted."""

from __future__ import annotations

import asyncio
import os
import smtplib
import time
from dataclasses import dataclass
from typing import Any

import httpx

from orchestrator.config import Settings
from orchestrator.connections.model import Connection
from orchestrator.connections.runtime import settings_for

TIMEOUT_S = 12.0


@dataclass
class CheckResult:
    #: True = works, False = does not, None = no automatic check exists / not contacted.
    ok: bool | None
    detail: str
    ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "detail": self.detail, "ms": self.ms}


async def check_connection(conn: Connection, settings: Settings) -> CheckResult:
    """Look at ``conn`` (saved or not). Never raises."""
    if not conn.configured:
        return CheckResult(False, f"{conn.spec.field(conn.spec.marker).label} is not filled in")
    if settings.dry_run:
        return CheckResult(None, "dry-run is on: nothing outside is contacted")
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(_check(conn, settings_for(settings, conn)), TIMEOUT_S)
    except TimeoutError:
        result = CheckResult(False, f"no answer in {int(TIMEOUT_S)} s")
    except Exception as exc:
        result = CheckResult(False, f"{type(exc).__name__}: {exc}"[:300])
    if result.ms is None and result.ok is not None:
        result.ms = int((time.monotonic() - started) * 1000)
    return result


async def _check(conn: Connection, s: Settings) -> CheckResult:
    kind = conn.kind
    if kind == "proxmox":
        from orchestrator.adapters.proxmox.api import LiveProxmoxAdapter

        a = LiveProxmoxAdapter(s)
        await a.start()
        try:
            v = await a.version()
        finally:
            await a.stop()
        return CheckResult(
            True, f"Proxmox VE {v.version}" + (f" ({v.release})" if v.release else "")
        )
    if kind == "pbs":
        from orchestrator.adapters.pbs.api import LivePbsAdapter

        p = LivePbsAdapter(s)
        await p.start()
        try:
            v = await p.version()
        finally:
            await p.stop()
        return CheckResult(True, f"Proxmox Backup Server {v.version}")
    if kind == "proxy":
        from orchestrator.adapters.proxy.traefik_manager import TraefikManagerProxyAdapter

        t = TraefikManagerProxyAdapter(s)
        await t.start()
        try:
            routes = await t.list_routes()
        finally:
            await t.stop()
        if routes is None:
            return CheckResult(False, "Traefik Manager did not answer (URL, API key, TLS?)")
        return CheckResult(True, f"Traefik Manager answered: {len(routes)} route(s)")
    if kind == "portainer":
        from orchestrator.adapters.portainer.api import LivePortainerAdapter

        pt = LivePortainerAdapter(s)
        await pt.start()
        try:
            c = await pt.check()
        finally:
            await pt.stop()
        return CheckResult(
            c.ok, c.detail or f"Portainer {c.version}, {c.environments} environment(s)"
        )
    if kind == "traefik_api":
        auth = (
            (s.traefik_api_username, s.traefik_api_password.get_secret_value())
            if s.traefik_api_username and s.traefik_api_password.get_secret_value()
            else None
        )
        async with httpx.AsyncClient(
            verify=s.traefik_api_verify_tls, timeout=s.traefik_api_timeout_s
        ) as c2:
            r = await c2.get(s.traefik_api_url.rstrip("/") + "/api/version", auth=auth)
        return CheckResult(r.status_code == 200, f"Traefik answered {r.status_code}")
    if kind == "haos":
        headers = {"Authorization": f"Bearer {s.haos_token.get_secret_value()}"}
        async with httpx.AsyncClient(verify=s.haos_verify_tls, timeout=10) as c3:
            r = await c3.get(s.haos_url.rstrip("/") + "/api/", headers=headers)
        return CheckResult(r.status_code == 200, f"Home Assistant answered {r.status_code}")
    if kind == "power":
        return await _tcp(s.mqtt_host, s.mqtt_port, "MQTT broker")
    if kind == "email":
        return await asyncio.to_thread(_smtp, s)
    if kind == "ssh":
        return _ssh(s)
    return CheckResult(
        None, "no automatic check - send a test notification from the Notifications page"
    )


async def _tcp(host: str, port: int, what: str) -> CheckResult:
    _r, w = await asyncio.open_connection(host, port)
    w.close()
    return CheckResult(True, f"{what} accepts connections on {host}:{port}")


def _smtp(s: Settings) -> CheckResult:
    with smtplib.SMTP(s.mail_smtp_host, s.mail_smtp_port, timeout=10) as smtp:
        smtp.ehlo()
        if s.mail_smtp_starttls:
            smtp.starttls()
            smtp.ehlo()
        if s.mail_smtp_username:
            smtp.login(s.mail_smtp_username, s.mail_smtp_password.get_secret_value())
    return CheckResult(True, f"SMTP server {s.mail_smtp_host} accepted the login")


def _ssh(s: Settings) -> CheckResult:
    notes: list[str] = []
    if s.ssh_key_path:
        if not os.path.isfile(s.ssh_key_path):
            return CheckResult(False, f"the private key {s.ssh_key_path} is not there")
        notes.append("key found")
    if s.host_runner_socket and not os.path.exists(s.host_runner_socket):
        return CheckResult(False, f"the host runner socket {s.host_runner_socket} is not there")
    if s.ssh_jump_host:
        notes.append(f"jump host {s.ssh_jump_host}:{s.ssh_jump_port} not tested")
    return CheckResult(True, ", ".join(notes) or "nothing to check")
