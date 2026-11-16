"""The QEMU guest agent calls the orchestrator may make, and nothing else.

The guest agent runs as root inside a VM, and Proxmox passes on whatever an API token is
allowed to ask it: with the wrong privilege that includes running commands and writing files.
The orchestrator only ever *reads* from it. This allow-list is the single place that says
what "read" means: :meth:`LiveProxmoxAdapter.agent_read` refuses any other command before a
request leaves the orchestrator, so no code path can reach exec, file access or a password
change by mistake. Adding a command here is a deliberate, reviewed change
(``tests/test_guest_agent_allowlist.py`` pins the list). See docs/guest_agent.md.
"""

from __future__ import annotations

from orchestrator.adapters.errors import AdapterError

#: command -> the HTTP method Proxmox's API uses for it. Proxmox exposes ``ping`` and
#: ``fsfreeze-status`` as POST, but neither changes the guest.
AGENT_READS: dict[str, str] = {
    "ping": "POST",
    "info": "GET",
    "network-get-interfaces": "GET",
    "get-users": "GET",
    "fsfreeze-status": "POST",
    "get-fsinfo": "GET",
    "get-osinfo": "GET",
}

#: Agent commands that would let whoever holds the token act inside the guest. The guest's own
#: lock-down should switch these off; ``info`` tells whether it did (guests/checks.py).
DANGEROUS_COMMANDS: frozenset[str] = frozenset(
    {
        "guest-exec",
        "guest-exec-status",
        "guest-file-open",
        "guest-file-close",
        "guest-file-read",
        "guest-file-write",
        "guest-file-seek",
        "guest-file-flush",
        "guest-set-user-password",
        "guest-ssh-add-authorized-keys",
        "guest-ssh-remove-authorized-keys",
    }
)


class AgentCallRefused(AdapterError):
    """A guest agent command outside :data:`AGENT_READS` was asked for. Never sent."""


def agent_method(command: str) -> str:
    """The HTTP method for an allowed read; raises :class:`AgentCallRefused` for anything else."""
    method = AGENT_READS.get(command)
    if method is None:
        raise AgentCallRefused(
            f"guest agent command {command!r} is not one the orchestrator may send "
            "(it only reads; see adapters/proxmox/agent.py)"
        )
    return method


__all__ = ["AGENT_READS", "DANGEROUS_COMMANDS", "AgentCallRefused", "agent_method"]
