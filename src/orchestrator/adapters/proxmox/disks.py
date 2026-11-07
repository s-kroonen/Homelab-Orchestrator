"""Reading how full a guest's filesystems are from what Proxmox answers.

A VM's disk is opaque to Proxmox; the QEMU guest agent's ``get-fsinfo`` lists what is mounted
inside it, with bytes used and total (agent 5.0 and later). A container's root filesystem is
measured by Proxmox itself and comes with the cluster's resource list.

Only real, writable storage is kept: pseudo and read-only image filesystems (tmpfs, overlay,
squashfs snaps, ISO images) are left out, and a filesystem mounted at several places (bind
mounts) is counted once, at its shortest mount point.
"""

from __future__ import annotations

from typing import Any

from orchestrator.adapters.proxmox.base import Filesystem

#: Filesystems that are not disk space a guest can run out of.
_SKIP_TYPES = {
    "autofs",
    "binfmt_misc",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "efivarfs",
    "fuse.lxcfs",
    "fusectl",
    "hugetlbfs",
    "iso9660",
    "mqueue",
    "nsfs",
    "overlay",
    "proc",
    "pstore",
    "ramfs",
    "securityfs",
    "squashfs",
    "sysfs",
    "tmpfs",
    "tracefs",
    "udf",
}


def parse_fsinfo(payload: Any) -> list[Filesystem]:
    """The guest agent's ``get-fsinfo`` answer (``{"result": [...]}`` or the list itself)."""
    rows = payload.get("result", []) if isinstance(payload, dict) else payload
    by_device: dict[str, Filesystem] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        fstype = str(row.get("type", "")).lower()
        mount = str(row.get("mountpoint", ""))
        total, used = row.get("total-bytes"), row.get("used-bytes")
        if fstype in _SKIP_TYPES or not mount or mount.startswith("/snap/"):
            continue
        if not isinstance(total, int) or not isinstance(used, int) or total <= 0:
            continue  # an older agent, or nothing to measure
        fs = Filesystem(mount=mount, fstype=fstype, used=max(0, used), total=total)
        device = str(row.get("name", "")) or mount
        known = by_device.get(device)
        if known is None or len(fs.mount) < len(known.mount):
            by_device[device] = fs
    return sorted(by_device.values(), key=lambda f: f.mount)


def lxc_root(disk: Any, maxdisk: Any) -> list[Filesystem]:
    """A container's root filesystem from the cluster's resource list (bytes)."""
    try:
        used, total = int(disk or 0), int(maxdisk or 0)
    except (TypeError, ValueError):
        return []
    return [Filesystem(mount="/", fstype="rootfs", used=used, total=total)] if total > 0 else []


__all__ = ["lxc_root", "parse_fsinfo"]
