"""Live Proxmox VE API adapter."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from orchestrator.adapters.errors import AdapterAuthError, AdapterError, TaskTimeout
from orchestrator.adapters.proxmox.base import (
    BackupStorage,
    ClusterNode,
    ClusterStatus,
    Guest,
    GuestNetwork,
    NodeHealth,
    ProxmoxAdapter,
    TaskHandle,
    TaskStatus,
    VersionInfo,
)
from orchestrator.adapters.proxmox.net import (
    parse_agent_interfaces,
    parse_lxc_interfaces,
    parse_lxc_net,
    parse_qemu_net_macs,
    qemu_agent_enabled,
)
from orchestrator.adapters.proxmox_family import ProxmoxFamilyClient
from orchestrator.config import Settings
from orchestrator.domain.enums import GuestKind
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

# Proxmox calls VMs "qemu" and containers "lxc" in its URL space.
_KIND_TO_ENDPOINT: dict[GuestKind, str] = {
    GuestKind.VM: "qemu",
    GuestKind.CT: "lxc",
}
_TYPE_TO_KIND: dict[str, GuestKind] = {
    "qemu": GuestKind.VM,
    "lxc": GuestKind.CT,
}


def _split_address(address: str, default_port: int) -> tuple[str, int]:
    """``host`` / ``host:port`` -> (host, port); a bare IPv6 literal keeps its colons."""
    value = address.strip()
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        if port.isdigit():
            return host, int(port)
    return value, default_port


class LiveProxmoxAdapter(ProxmoxAdapter):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = ProxmoxFamilyClient(
            flavor="pve",
            host=settings.proxmox_host,
            port=settings.proxmox_port,
            token_id=settings.proxmox_token_id,
            token_secret=settings.proxmox_token_secret.get_secret_value(),
            verify_tls=settings.proxmox_verify_tls,
        )

    async def start(self) -> None:
        await self._client.start()

    async def stop(self) -> None:
        await self._client.stop()

    async def version(self) -> VersionInfo:
        data = await self._client.request("GET", "/version")
        data = data or {}
        return VersionInfo(
            version=str(data.get("version", "")),
            release=str(data.get("release", "")),
            raw=data,
        )

    async def node_health(self, node: str, address: str) -> NodeHealth:
        host, port = _split_address(address, self._settings.proxmox_port)
        s = self._settings
        client = ProxmoxFamilyClient(
            flavor="pve",
            host=host,
            port=port,
            token_id=s.proxmox_token_id,
            token_secret=s.proxmox_token_secret.get_secret_value(),
            verify_tls=s.proxmox_verify_tls,
            connect_timeout=4.0,
            read_timeout=6.0,
        )
        started = time.monotonic()
        try:
            data = await client.request("GET", "/version") or {}
            latency = round((time.monotonic() - started) * 1000, 1)
            version = str(data.get("version", ""))
            uptime = cpu = mem_used = mem_total = None
            try:
                # Needs Sys.Audit; a token without it still proves the node is up.
                status = await client.request("GET", f"/nodes/{node}/status") or {}
                uptime = int(status["uptime"]) if "uptime" in status else None
                cpu = float(status["cpu"]) if "cpu" in status else None
                mem = status.get("memory") or {}
                mem_used = int(mem["used"]) if "used" in mem else None
                mem_total = int(mem["total"]) if "total" in mem else None
            except (AdapterError, KeyError, TypeError, ValueError):
                pass
            return NodeHealth(
                reachable=True,
                latency_ms=latency,
                version=version,
                uptime_s=uptime,
                cpu=cpu,
                memory_used=mem_used,
                memory_total=mem_total,
            )
        except AdapterError as exc:
            return NodeHealth(reachable=False, error=str(exc))
        finally:
            await client.stop()

    async def list_guests(self, node: str | None = None) -> list[Guest]:
        rows = await self._client.request("GET", "/cluster/resources", params={"type": "vm"})
        guests: list[Guest] = []
        for row in rows or []:
            kind = _TYPE_TO_KIND.get(str(row.get("type", "")))
            if kind is None:
                continue
            if node is not None and row.get("node") != node:
                continue
            guests.append(
                Guest(
                    node=str(row.get("node", "")),
                    vmid=int(row.get("vmid", 0)),
                    kind=kind,
                    name=str(row.get("name", "")),
                    status=str(row.get("status", "unknown")),
                )
            )
        return sorted(guests, key=lambda g: g.vmid)

    async def cluster_status(self) -> ClusterStatus:
        rows = await self._client.request("GET", "/cluster/status") or []
        nodes: list[ClusterNode] = []
        quorate: bool | None = None
        cluster_name = ""
        for row in rows:
            if row.get("type") == "cluster":
                quorate = bool(row.get("quorate"))
                cluster_name = str(row.get("name", ""))
            elif row.get("type") == "node":
                nodes.append(
                    ClusterNode(
                        name=str(row.get("name", "")),
                        online=bool(row.get("online")),
                        local=bool(row.get("local")),
                        ip=str(row.get("ip", "")),
                    )
                )
        return ClusterStatus(
            nodes=sorted(nodes, key=lambda n: n.name),
            quorate=quorate,
            cluster_name=cluster_name,
        )

    async def list_backup_storages(self) -> list[BackupStorage]:
        rows = await self._client.request("GET", "/storage", params={"type": "pbs"}) or []
        storages = [
            BackupStorage(
                storage=str(row.get("storage", "")),
                datastore=str(row.get("datastore", "")),
                server=str(row.get("server", "")),
            )
            for row in rows
            if row.get("type", "pbs") == "pbs"
        ]
        return sorted(storages, key=lambda s: s.storage)

    async def guest_network(self, node: str, vmid: int, kind: GuestKind) -> GuestNetwork:
        """MACs from the guest config (needs VM.Audit), IPs from the QEMU guest
        agent (needs VM.Monitor) or the container's config / interfaces.

        Permission problems and a missing agent degrade to an answer with no
        IPs instead of raising — the caller then tries ARP / DHCP with the MACs.
        """
        endpoint = self._endpoint_for(kind)
        base = f"/nodes/{node}/{endpoint}/{vmid}"
        try:
            config = await self._client.request("GET", f"{base}/config") or {}
        except AdapterAuthError as exc:
            return GuestNetwork(
                error=f"reading the {endpoint} config was refused ({exc.status_code}) — the "
                f"API token needs VM.Audit on /vms/{vmid}"
            )
        except AdapterError as exc:
            return GuestNetwork(error=f"could not read the {endpoint} config: {exc}")

        if kind is GuestKind.CT:
            macs, static_ips = parse_lxc_net(config)
            if static_ips:
                return GuestNetwork(macs=macs, ips=static_ips, source="lxc")
            try:
                rows = await self._client.request("GET", f"{base}/interfaces")
            except AdapterAuthError as exc:
                return GuestNetwork(
                    macs=macs,
                    error=f"reading the container's interfaces was refused "
                    f"({exc.status_code}) — the API token needs VM.Audit on /vms/{vmid}",
                )
            except AdapterError as exc:
                # Older PVE (no /interfaces) or a stopped container: a miss.
                log.debug("proxmox.lxc_interfaces.unavailable", vmid=vmid, error=str(exc))
                return GuestNetwork(macs=macs)
            ips = parse_lxc_interfaces(rows, macs)
            return GuestNetwork(macs=macs, ips=ips, source="lxc" if ips else None)

        macs = parse_qemu_net_macs(config)
        if not qemu_agent_enabled(config):
            return GuestNetwork(macs=macs)
        try:
            payload = await self._client.request("GET", f"{base}/agent/network-get-interfaces")
        except AdapterAuthError as exc:
            return GuestNetwork(
                macs=macs,
                error=f"the guest agent read was refused ({exc.status_code}) — the API "
                f"token needs VM.Monitor on /vms/{vmid}",
            )
        except AdapterError as exc:
            # "QEMU guest agent is not running" (500) is the normal miss.
            log.debug("proxmox.guest_agent.unavailable", vmid=vmid, error=str(exc))
            return GuestNetwork(macs=macs)
        ips = parse_agent_interfaces(payload, macs)
        return GuestNetwork(macs=macs, ips=ips, source="guest_agent" if ips else None)

    async def start_guest(self, node: str, vmid: int, kind: GuestKind) -> TaskHandle:
        endpoint = self._endpoint_for(kind)
        upid = await self._client.request("POST", f"/nodes/{node}/{endpoint}/{vmid}/status/start")
        log.info("proxmox.start_guest", node=node, vmid=vmid, upid=upid)
        return TaskHandle(node=node, upid=str(upid))

    async def stop_guest(self, node: str, vmid: int, kind: GuestKind) -> TaskHandle:
        endpoint = self._endpoint_for(kind)
        upid = await self._client.request(
            "POST", f"/nodes/{node}/{endpoint}/{vmid}/status/shutdown"
        )
        log.info("proxmox.stop_guest", node=node, vmid=vmid, upid=upid)
        return TaskHandle(node=node, upid=str(upid))

    async def vzdump(
        self,
        *,
        node: str,
        vmid: int,
        storage: str,
        mode: str,
        notes: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> TaskHandle:
        payload: dict[str, Any] = {
            "vmid": vmid,
            "storage": storage,
            "mode": mode,
        }
        if notes:
            # PBS shows this on the snapshot; makes orchestrator-made backups
            # identifiable in the PBS UI.
            payload["notes-template"] = notes
        if extra:
            payload.update(extra)

        upid = await self._client.request("POST", f"/nodes/{node}/vzdump", data=payload)
        log.info(
            "proxmox.vzdump.started",
            node=node,
            vmid=vmid,
            storage=storage,
            mode=mode,
            upid=upid,
        )
        return TaskHandle(node=node, upid=str(upid))

    async def restore_guest(
        self, *, node: str, vmid: int, kind: GuestKind, archive: str
    ) -> TaskHandle:
        s = self._settings
        if not s.proxmox_restore_token_id.strip():
            raise AdapterAuthError(
                "restoring needs the restore token: set PROXMOX_RESTORE_TOKEN_ID and "
                "PROXMOX_RESTORE_TOKEN_SECRET (docs/api_tokens.md)"
            )
        client = ProxmoxFamilyClient(
            flavor="pve",
            host=s.proxmox_host,
            port=s.proxmox_port,
            token_id=s.proxmox_restore_token_id,
            token_secret=s.proxmox_restore_token_secret.get_secret_value(),
            verify_tls=s.proxmox_verify_tls,
        )
        endpoint = self._endpoint_for(kind)
        data: dict[str, Any] = {"vmid": vmid, "force": 1}
        if kind is GuestKind.VM:
            data["archive"] = archive
        else:
            data.update({"ostemplate": archive, "restore": 1})
        try:
            upid = await client.request("POST", f"/nodes/{node}/{endpoint}", data=data)
        finally:
            await client.stop()
        log.warning("proxmox.restore.started", node=node, vmid=vmid, archive=archive, upid=upid)
        return TaskHandle(node=node, upid=str(upid))

    async def task_status(self, node: str, upid: str) -> TaskStatus:
        status = await self._client.request("GET", f"/nodes/{node}/tasks/{upid}/status")
        status = status or {}

        log_tail: list[str] = []
        try:
            entries = await self._client.request(
                "GET", f"/nodes/{node}/tasks/{upid}/log", params={"limit": 50}
            )
            log_tail = [str(e.get("t", "")) for e in entries or []]
        except Exception as exc:  # log is a nicety — never fail the poll over it
            log.debug("proxmox.task_log.unavailable", upid=upid, error=str(exc))

        return TaskStatus(
            upid=upid,
            status=str(status.get("status", "unknown")),
            exit_status=status.get("exitstatus"),
            log_tail=log_tail,
        )

    async def wait_for_task(
        self,
        node: str,
        upid: str,
        *,
        timeout_s: int = 3600,
        poll_interval_s: float = 3.0,
    ) -> TaskStatus:
        deadline = time.monotonic() + timeout_s
        while True:
            status = await self.task_status(node, upid)
            if status.finished:
                log.info(
                    "proxmox.task.finished",
                    upid=upid,
                    exit_status=status.exit_status,
                    succeeded=status.succeeded,
                )
                return status
            if time.monotonic() >= deadline:
                raise TaskTimeout(f"task {upid} still running after {timeout_s}s", upid=upid)
            await asyncio.sleep(poll_interval_s)

    @staticmethod
    def _endpoint_for(kind: GuestKind) -> str:
        endpoint = _KIND_TO_ENDPOINT.get(kind)
        if endpoint is None:
            raise ValueError(f"guest kind {kind!r} has no Proxmox lifecycle endpoint")
        return endpoint
