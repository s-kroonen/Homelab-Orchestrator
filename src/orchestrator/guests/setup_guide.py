"""What is still missing before a guest's agent can be read, and how to fix it.

Built from one :class:`GuestFacts`: a checklist (may the orchestrator ask? is the option on?
does it answer? is it locked down?) and only the steps still needed, fitted to the guest's OS
(the source's OS type, or what the agent said). Proxmox wording, since Proxmox is the only
source with an agent today; another source brings its own guide.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from orchestrator.guests.model import AgentState, GuestFacts

#: The lock-down list the steps write (the dangerous commands, plus reading SSH keys).
BLOCK_RPCS = ",".join(
    [
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
        "guest-ssh-get-authorized-keys",
    ]
)

GRANT_COMMAND = 'pveum role modify OrchestratorBackup --append 1 --privs "VM.GuestAgent.Audit"'
LINUX_LOCKDOWN = (
    f"printf '[general]\\nblock-rpcs={BLOCK_RPCS}\\n' > /etc/qemu/qemu-ga.conf\n"
    "systemctl restart qemu-guest-agent"
)


@dataclass
class Check:
    label: str
    #: True done, False missing, None not known yet.
    ok: bool | None
    value: str
    hint: str = ""


@dataclass
class Step:
    title: str
    text: str = ""
    command: str = ""
    note: str = ""


@dataclass
class Guide:
    os_family: str  # linux | windows
    complete: bool
    summary: str
    checks: list[Check] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def os_family(facts: GuestFacts) -> str:
    kind = (facts.os_type or "").lower()
    name = (facts.os_name or "").lower()
    if kind.startswith("win") or "windows" in name:
        return "windows"
    return "linux"


def _install(facts: GuestFacts) -> Step:
    if os_family(facts) == "windows":
        return Step(
            "Install it in the guest",
            "Run the VirtIO guest tools installer from the virtio-win ISO; it includes the "
            "QEMU Guest Agent service.",
        )
    name = (facts.os_name or "").lower()
    if any(x in name for x in ("fedora", "red hat", "rhel", "centos", "rocky", "alma")):
        command = "dnf install qemu-guest-agent"
    elif "alpine" in name:
        command = "apk add qemu-guest-agent"
    elif "arch" in name:
        command = "pacman -S qemu-guest-agent"
    else:
        command = "apt install qemu-guest-agent"
    note = (
        ""
        if facts.os_name
        else "Fedora/RHEL: dnf install qemu-guest-agent · Alpine: apk add qemu-guest-agent · "
        "FreeBSD/pfSense: pkg install qemu-guest-agent"
    )
    return Step("Install it in the guest", command=command, note=note)


def _lockdown(facts: GuestFacts) -> Step:
    if os_family(facts) == "windows":
        return Step(
            "Lock it down",
            "Add -b with this list to the QEMU Guest Agent service's start parameters, then "
            "restart the service.",
            command=f"-b {BLOCK_RPCS}",
            note="It can then answer questions but not run anything.",
        )
    return Step(
        "Lock it down",
        "It can then answer questions but not run anything.",
        command=LINUX_LOCKDOWN,
        note="Check the names on your agent version with: qemu-ga --block-rpcs=help",
    )


def _enable(facts: GuestFacts) -> Step:
    vmid = facts.ref.key.partition("/")[2] or "?"
    return Step(
        "Turn it on in Proxmox",
        f"VM {vmid} → Options → QEMU Guest Agent → Use QEMU Guest Agent, and Freeze/thaw "
        "guest filesystems on backup.",
    )


_RESTART = Step(
    "Stop and start the VM once",
    "A reboot from inside the guest is not enough: the option adds a device. Then press "
    "Read now.",
)


def build(facts: GuestFacts) -> Guide:
    family = os_family(facts)
    state = facts.agent
    may_read = Check("The orchestrator may read guest agents", None, "", "VM.GuestAgent.Audit")
    option = Check("Agent option on in Proxmox", None, "")
    answers = Check("Agent answers", None, "")
    locked = Check("Locked down", None, "after it answers")

    if state is AgentState.NO_PERMISSION:
        may_read.ok, may_read.value = False, "refused"
        option.ok, option.value = True, "on"
        answers.value = "after the fix"
        locked.value = "after the fix"
        vmid = facts.ref.key.partition("/")[2]
        return Guide(
            family,
            False,
            "Proxmox refused the agent read (403 Forbidden). The agent itself may be fine: the "
            "orchestrator is not allowed to ask it.",
            [may_read, option, answers, locked],
            [
                Step(
                    "Let the orchestrator read guest agents",
                    "On a Proxmox node, once: it applies to every VM the token may see.",
                    command=GRANT_COMMAND,
                    note="Proxmox 8: use VM.Monitor instead. Your role may have another name: "
                    "it is the one the orchestrator's token uses.",
                ),
                Step(
                    "Press Read now",
                    "No restart needed. If it is still refused, the token's ACL does not cover "
                    f"/vms/{vmid}.",
                ),
            ],
        )
    if state is AgentState.NOT_ENABLED:
        option.ok, option.value = False, "off"
        answers.ok, answers.value = False, "no"
        may_read.value = "checked once it is on"
        return Guide(
            family,
            False,
            "Set up the guest agent to see this VM's address, disks, network and who is signed "
            "in. About 5 minutes and one restart.",
            [may_read, option, answers, locked],
            [_install(facts), _lockdown(facts), _enable(facts), _RESTART],
        )
    if state is AgentState.NOT_ANSWERING:
        may_read.ok, may_read.value = None, "checked once it answers"
        option.ok, option.value = True, "on"
        answers.ok, answers.value = False, "no"
        steps = [_install(facts), _lockdown(facts), _RESTART]
        steps[-1] = Step(
            "Make sure it runs",
            "Linux: systemctl enable --now qemu-guest-agent. If the option was only just "
            "switched on, stop and start the VM once. Then press Read now.",
        )
        return Guide(
            family,
            False,
            "The agent option is on, but nothing answers: the agent is not installed, not "
            "running, or the guest is stuck.",
            [may_read, option, answers, locked],
            steps,
        )
    if state is AgentState.ANSWERS:
        may_read.ok, may_read.value = True, "yes"
        option.ok, option.value = True, "on"
        answers.ok, answers.value = True, f"{facts.ping_ms:g} ms" if facts.ping_ms else "yes"
        if facts.locked_down is False:
            locked.ok, locked.value = False, ", ".join(facts.dangerous_enabled) + " on"
            return Guide(
                family,
                False,
                "The agent answers, but it also accepts commands. The orchestrator never sends "
                "them; lock it down so nobody else can.",
                [may_read, option, answers, locked],
                [_lockdown(facts)],
            )
        locked.ok = facts.locked_down
        locked.value = "yes" if facts.locked_down else "not known"
        return Guide(family, True, "Set up.", [may_read, option, answers, locked], [])
    summary = {
        AgentState.NOT_RUNNING: "The guest is not running: its agent is read when it runs.",
        AgentState.NOT_NEEDED: "Proxmox reads containers itself: no agent needed.",
    }.get(state, facts.agent_detail or "Not known yet: it is read on the next health cycle.")
    return Guide(family, state is AgentState.NOT_NEEDED, summary, [], [])


__all__ = ["BLOCK_RPCS", "GRANT_COMMAND", "Guide", "build", "os_family"]
