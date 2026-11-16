"""Reading a Proxmox guest: its config from Proxmox, everything else from its QEMU guest agent.

Every agent call goes through :meth:`ProxmoxAdapter.agent_read`, which only sends the
read-only commands in ``adapters/proxmox/agent.py``. A container has no agent to ask: Proxmox
reads it itself, so only its addresses are taken (from its config / interfaces).

The order matters for the answer the screens give: the guest's config first (is the agent
option on?), then ``ping`` (may the token ask it, and does it answer?), and only then the
rest, each on its own: one command failing does not lose the others.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from orchestrator.adapters.errors import AdapterAuthError, AdapterError, AdapterUnreachable
from orchestrator.adapters.proxmox.disks import parse_fsinfo
from orchestrator.adapters.proxmox.net import (
    normalize_mac,
    parse_agent_interfaces,
    parse_qemu_net_macs,
    qemu_agent_enabled,
    usable_ip,
)
from orchestrator.domain.enums import GuestKind
from orchestrator.guests.model import (
    AgentState,
    GuestFacts,
    GuestTarget,
    Interface,
    SignedIn,
    now_iso,
)
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

_DEFAULT_TIMEOUT_S = 5.0


def permission_detail(status_code: int | None, vmid: int | None) -> str:
    return (
        f"Proxmox refused the agent read ({status_code or 403}): the orchestrator's API token "
        f"needs VM.GuestAgent.Audit (VM.Monitor before Proxmox 9) on /vms/{vmid}"
    )


class ProxmoxGuestProvider:
    """:class:`~orchestrator.guests.model.GuestFactsProvider` for one Proxmox cluster."""

    def __init__(self, proxmox: Any, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self._proxmox = proxmox
        self._timeout = timeout_s

    async def read(
        self, target: GuestTarget, previous: GuestFacts | None, *, os_due: bool
    ) -> GuestFacts:
        facts = GuestFacts(
            ref=target.ref,
            name=target.name,
            kind=target.kind,
            running=target.running,
            read_at=now_iso(),
        )
        try:
            if target.kind == GuestKind.CT.value:
                await self._read_container(target, facts)
            else:
                await self._read_vm(target, previous, facts, os_due=os_due)
        except Exception as exc:  # a provider never raises (model.GuestFactsProvider)
            log.warning("guests.proxmox.read_failed", guest=str(target.ref), error=str(exc))
            facts.agent = AgentState.UNKNOWN
            facts.agent_detail = f"{type(exc).__name__}: {exc}"
        return facts

    # -- containers ------------------------------------------------------------------------

    async def _read_container(self, target: GuestTarget, facts: GuestFacts) -> None:
        facts.agent = AgentState.NOT_NEEDED
        facts.agent_detail = "Proxmox reads containers itself"
        if not target.running or target.vmid is None:
            return
        net = await self._proxmox.guest_network(target.node, target.vmid, GuestKind.CT)
        facts.macs = list(net.macs)
        facts.addresses = list(net.ips)
        facts.address_source = net.source
        if net.error:
            facts.errors["network"] = net.error

    # -- VMs -------------------------------------------------------------------------------

    async def _read_vm(
        self,
        target: GuestTarget,
        previous: GuestFacts | None,
        facts: GuestFacts,
        *,
        os_due: bool,
    ) -> None:
        vmid = target.vmid
        if vmid is None:
            facts.agent_detail = "no VM id"
            return
        if previous is not None:
            # Kept until read again: the OS (read daily) and the last known addresses (a guest
            # that stopped answering has not necessarily moved).
            facts.os_name, facts.os_version = previous.os_name, previous.os_version
            facts.kernel, facts.os_read_at = previous.kernel, previous.os_read_at
            facts.addresses, facts.address_source = (
                list(previous.addresses),
                previous.address_source,
            )
        if not target.running:
            facts.agent = AgentState.NOT_RUNNING
            facts.agent_detail = "the VM is not running"
            return

        try:
            config = await self._call(self._proxmox.guest_config(target.node, vmid, GuestKind.VM))
        except AdapterAuthError as exc:
            facts.agent = AgentState.UNKNOWN
            facts.agent_detail = (
                f"reading the VM's config was refused ({exc.status_code}): the API token "
                f"needs VM.Audit on /vms/{vmid}"
            )
            return
        except (AdapterError, TimeoutError) as exc:
            facts.agent = AgentState.UNKNOWN
            facts.agent_detail = f"could not read the VM's config: {exc or 'timed out'}"
            return
        facts.macs = parse_qemu_net_macs(config)
        facts.os_type = str(config.get("ostype") or "") or None
        enabled = qemu_agent_enabled(config)
        facts.freeze_on_backup = _freeze_on_backup(config) if enabled else None
        if not enabled:
            facts.agent = AgentState.NOT_ENABLED
            facts.agent_detail = "the QEMU Guest Agent option is off for this VM in Proxmox"
            return

        started = time.monotonic()
        try:
            await self._agent(target, "ping")
        except AdapterAuthError as exc:
            facts.agent = AgentState.NO_PERMISSION
            facts.agent_detail = permission_detail(exc.status_code, vmid)
            return
        except (AdapterError, TimeoutError) as exc:
            if isinstance(exc, AdapterUnreachable):
                facts.agent = AgentState.UNKNOWN
                facts.agent_detail = f"Proxmox did not answer: {exc}"
                return
            facts.agent = AgentState.NOT_ANSWERING
            facts.misses = (previous.misses if previous else 0) + 1
            facts.agent_detail = (
                "the agent did not answer: is qemu-guest-agent installed and running in the "
                f"guest? ({exc or 'timed out'})"
            )
            return
        facts.agent = AgentState.ANSWERS
        facts.ping_ms = round((time.monotonic() - started) * 1000, 1)

        info = await self._try(target, facts, "info")
        if isinstance(info, dict):
            facts.agent_version = str(info.get("version") or "") or None
            facts.enabled_commands = sorted(
                str(c.get("name"))
                for c in info.get("supported_commands") or []
                if isinstance(c, dict) and c.get("enabled") and c.get("name")
            )
        net = await self._try(target, facts, "network-get-interfaces")
        if net is not None:
            facts.interfaces = parse_interfaces(net)
            facts.addresses = parse_agent_interfaces(net, facts.macs)
            facts.address_source = "guest_agent" if facts.addresses else None
        users = await self._try(target, facts, "get-users")
        if users is not None:
            facts.sessions = parse_users(users)
        freeze = await self._try(target, facts, "fsfreeze-status")
        if isinstance(freeze, str):
            facts.freeze = freeze.strip().lower() or None
        fsinfo = await self._try(target, facts, "get-fsinfo")
        if fsinfo is not None:
            facts.filesystems = [
                {"mount": f.mount, "fstype": f.fstype, "used": f.used, "total": f.total}
                for f in parse_fsinfo(fsinfo)
            ]
        elif "get-fsinfo" in facts.errors:
            facts.filesystems_error = facts.errors["get-fsinfo"]
        if os_due or facts.os_name is None:
            osinfo = await self._try(target, facts, "get-osinfo")
            if isinstance(osinfo, dict):
                facts.os_name = str(osinfo.get("name") or osinfo.get("id") or "") or None
                facts.os_version = str(osinfo.get("version-id") or osinfo.get("version") or "")
                facts.kernel = str(osinfo.get("kernel-release") or "") or None
                facts.os_read_at = now_iso()

    # -- calls -----------------------------------------------------------------------------

    async def _call(self, coro: Any) -> Any:
        return await asyncio.wait_for(coro, self._timeout)

    async def _agent(self, target: GuestTarget, command: str) -> Any:
        assert target.vmid is not None
        return await self._call(self._proxmox.agent_read(target.node, target.vmid, command))

    async def _try(self, target: GuestTarget, facts: GuestFacts, command: str) -> Any:
        """One command; a failure is noted on the facts and the reading goes on."""
        try:
            return await self._agent(target, command)
        except (AdapterError, TimeoutError) as exc:
            facts.errors[command] = str(exc) or "timed out"
            return None


# -- parsing --------------------------------------------------------------------------------


def _freeze_on_backup(config: dict[str, Any]) -> bool:
    """``agent: 1,freeze-fs-on-backup=0`` -> False. Proxmox's default is on."""
    for part in str(config.get("agent", "")).split(","):
        key, _, value = part.strip().partition("=")
        if key == "freeze-fs-on-backup":
            return value.strip() not in ("0", "false", "no", "off")
    return True


def parse_interfaces(payload: Any) -> list[Interface]:
    """``network-get-interfaces`` -> interfaces with their usable addresses and counters.
    Loopback is left out."""
    rows = payload.get("result") if isinstance(payload, dict) else payload
    out: list[Interface] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "")).strip()
        if not name or name == "lo":
            continue
        addresses: list[str] = []
        for addr in row.get("ip-addresses") or []:
            if not isinstance(addr, dict):
                continue
            ip = usable_ip(addr.get("ip-address"))
            if ip:
                prefix = addr.get("prefix")
                addresses.append(f"{ip}/{prefix}" if isinstance(prefix, int) else ip)
        stats = row.get("statistics") if isinstance(row.get("statistics"), dict) else {}
        out.append(
            Interface(
                name=name,
                mac=normalize_mac(row.get("hardware-address")) or "",
                addresses=addresses,
                rx_bytes=_int_or_none(stats.get("rx-bytes")),
                tx_bytes=_int_or_none(stats.get("tx-bytes")),
            )
        )
    return out


def parse_users(payload: Any) -> list[SignedIn]:
    """``get-users`` -> who is signed in, earliest first. ``login-time`` is seconds since the
    epoch (a float)."""
    rows = payload.get("result") if isinstance(payload, dict) else payload
    out: list[SignedIn] = []
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("user"):
            continue
        try:
            since = datetime.fromtimestamp(float(row.get("login-time") or 0), UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        out.append(
            SignedIn(
                user=str(row["user"]), since=since.isoformat(), domain=str(row.get("domain") or "")
            )
        )
    return sorted(out, key=lambda s: s.since)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


__all__ = ["ProxmoxGuestProvider", "parse_interfaces", "parse_users", "permission_detail"]
