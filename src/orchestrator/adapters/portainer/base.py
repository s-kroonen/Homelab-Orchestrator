"""The Portainer adapter interface and the shapes it returns.

Portainer owns stacks the way Proxmox owns guests: this adapter *reads* them, and the
orchestrator keeps only links to them. Every shape here is therefore already
**redacted**: stack environment values, Git credentials, container environments and
webhook ids never appear in them - only the names, and "there is one".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from orchestrator.adapters.errors import AdapterRequestError


@dataclass(frozen=True)
class PortainerCheck:
    """One answer to "is Portainer usable?" for the connectivity checks."""

    configured: bool
    reachable: bool
    ok: bool
    version: str = ""
    edition: str = ""
    environments: int = 0
    detail: str = ""


@dataclass(frozen=True)
class Environment:
    id: int
    name: str
    #: docker | agent | edge | kubernetes | azure | unknown
    type: str
    #: up | down | unknown
    status: str
    url: str = ""
    running: int | None = None
    stopped: int | None = None
    healthy: int | None = None
    unhealthy: int | None = None
    stacks: int | None = None

    @property
    def is_docker(self) -> bool:
        return self.type in {"docker", "agent", "edge"}


@dataclass(frozen=True)
class GitInfo:
    url: str  # credentials stripped
    reference: str = ""
    path: str = ""
    hash: str = ""
    has_credentials: bool = False


@dataclass(frozen=True)
class Stack:
    #: Portainer's id; None for a compose project Portainer did not create (it is only
    #: visible through its containers' labels).
    id: int | None
    name: str
    endpoint_id: int
    #: compose | swarm | kubernetes | unknown
    type: str
    #: active | inactive
    status: str
    managed: bool = True
    git: GitInfo | None = None
    auto_update: bool = False
    #: How often Portainer polls the repository ("5m"), blank when it does not.
    auto_update_interval: str = ""
    #: Portainer has a deploy webhook for this stack (the id itself is never exposed).
    has_webhook: bool = False
    #: Names only. Values are never read out of the client.
    env_names: tuple[str, ...] = ()
    created_at: str | None = None
    updated_at: str | None = None
    created_by: str = ""
    updated_by: str = ""


@dataclass(frozen=True)
class PortMapping:
    private: int
    proto: str = "tcp"
    public: int | None = None
    ip: str = ""


@dataclass(frozen=True)
class Container:
    id: str
    name: str
    #: running | exited | paused | restarting | created | dead | removing
    state: str
    #: Docker's own words, e.g. "Up 2 hours (healthy)"
    status_text: str = ""
    #: healthy | unhealthy | starting | "" (no health check)
    health: str = ""
    image: str = ""
    image_id: str = ""
    created_at: str | None = None
    ports: tuple[PortMapping, ...] = ()
    #: The compose project / swarm stack it belongs to, and its compose service name.
    stack: str | None = None
    service: str | None = None


@dataclass(frozen=True)
class Mount:
    type: str
    source: str
    destination: str
    read_only: bool = False


@dataclass(frozen=True)
class ContainerDetail:
    container: Container
    restart_count: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    restart_policy: str = ""
    health_log: tuple[str, ...] = ()
    mounts: tuple[Mount, ...] = ()
    networks: tuple[str, ...] = ()
    tty: bool = False
    #: Names of the environment variables only; never the values.
    env_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class LogLine:
    ts: str | None
    stream: str  # stdout | stderr
    text: str


@dataclass(frozen=True)
class ContainerStats:
    cpu_percent: float
    mem_bytes: int
    mem_limit: int
    net_rx_bytes: int = 0
    net_tx_bytes: int = 0
    pids: int = 0


@dataclass(frozen=True)
class StackCreate:
    """What to ask Portainer to create. Passed straight through to Portainer: the
    environment values and any Git password are sent and never stored, logged or echoed
    (see :meth:`PortainerAdapter.preview_create`)."""

    endpoint_id: int
    name: str
    #: git | string
    source: str = "git"
    # -- source = git
    repository_url: str = ""
    reference: str = "refs/heads/main"
    compose_path: str = "docker-compose.yml"
    #: A Git credential Portainer already holds (its id), preferred over a password here.
    git_credential_id: int | None = None
    git_username: str = ""
    git_password: str = ""
    #: Portainer's own auto-update (poll the repository / create its deploy webhook).
    auto_update: bool = False
    auto_update_interval: str = ""
    # -- source = string
    compose_text: str = ""
    # -- both: (name, value) pairs
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ExecResult:
    """How a command run inside a container ended. The output went to the caller's sink."""

    exit_code: int | None
    #: The tail of what the command wrote to stderr (its own explanation of a failure).
    stderr: str = ""
    stdout_bytes: int = 0


@dataclass
class StackListing:
    stacks: list[Stack] = field(default_factory=list)
    #: environment id -> why its containers could not be read (the rest is still shown)
    errors: dict[int, str] = field(default_factory=dict)


class PortainerAdapter(ABC):
    IMPLEMENTED = True
    #: The connection this adapter serves (set when it is built from one).
    instance_id: str = "portainer"
    instance_name: str = "Portainer"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @property
    @abstractmethod
    def configured(self) -> bool: ...

    @property
    @abstractmethod
    def base_url(self) -> str:
        """The address people open in a browser (PORTAINER_PUBLIC_URL, else PORTAINER_URL)."""

    @abstractmethod
    async def check(self) -> PortainerCheck: ...

    @abstractmethod
    async def list_environments(self) -> list[Environment]: ...

    @abstractmethod
    async def list_stacks(self, endpoint_id: int | None = None) -> StackListing: ...

    @abstractmethod
    async def get_stack(self, endpoint_id: int, name: str) -> Stack | None: ...

    # -- writes (every one refuses when PORTAINER_READ_ONLY=true) ------------------------

    @abstractmethod
    async def start_stack(self, stack: Stack) -> None: ...

    @abstractmethod
    async def stop_stack(self, stack: Stack) -> None: ...

    @abstractmethod
    async def redeploy_stack(
        self, stack: Stack, *, pull_image: bool = True, prune: bool = False
    ) -> None:
        """Re-deploy a Git stack from its repository. Portainer replaces the stack's
        environment with what the request carries, so the current one is read and sent
        back unchanged (never exposed)."""

    @abstractmethod
    def preview_create(self, spec: StackCreate) -> dict[str, Any]:
        """The exact request :meth:`create_stack` would send, with secrets masked, so it
        can be shown and confirmed first."""

    @abstractmethod
    async def create_stack(self, spec: StackCreate) -> Stack: ...

    @abstractmethod
    async def delete_stack(self, stack: Stack) -> None:
        """Delete the stack in Portainer. Volumes are kept."""

    @abstractmethod
    async def container_action(self, endpoint_id: int, ref: str, verb: str) -> str:
        """``start`` | ``stop`` | ``restart`` | ``kill`` | ``recreate`` (pull its image and
        create it again) one container (by id or name). Returns a short message; raises when
        it is not found or Portainer refuses."""

    @abstractmethod
    async def exec_stream(
        self,
        endpoint_id: int,
        ref: str,
        cmd: list[str],
        sink: Callable[[bytes], None],
        *,
        timeout_s: float = 1800.0,
    ) -> ExecResult:
        """Run ``cmd`` inside a running container and hand its stdout to ``sink`` as it
        arrives (a dump can be larger than memory). The command's own environment is the
        container's, so credentials never pass through here. Refuses when
        ``PORTAINER_READ_ONLY=true``."""

    @abstractmethod
    async def stack_kind(self, stack: Stack) -> str:
        """``image`` | ``build`` | ``mixed`` | ``unknown`` - read from the compose file,
        which is parsed and discarded, never returned."""

    @abstractmethod
    async def list_containers(
        self, endpoint_id: int, *, stack: str | None = None, include_stopped: bool = True
    ) -> list[Container]: ...

    @abstractmethod
    async def get_container(self, endpoint_id: int, ref: str) -> ContainerDetail | None:
        """``ref`` is a container id (any prefix) or its name."""

    @abstractmethod
    async def logs(
        self,
        endpoint_id: int,
        ref: str,
        *,
        tail: int = 200,
        since: int | None = None,
        timestamps: bool = True,
    ) -> list[LogLine]: ...

    @abstractmethod
    async def stats(self, endpoint_id: int, ref: str) -> ContainerStats: ...

    # -- the stack page: environment, Git settings, container inspect ---------------------------
    # Not abstract: an adapter without them (a test double) refuses, it does not break.

    async def stack_env(self, stack: Stack) -> list[tuple[str, str]]:
        """The stack's environment, values included. Only the stack page asks for it, and it
        never returns a secret-looking value unless one is revealed on purpose (audited)."""
        raise AdapterRequestError("reading a stack's environment is not supported here")

    async def set_stack_env(
        self,
        stack: Stack,
        env: list[tuple[str, str]],
        *,
        pull_image: bool = True,
        prune: bool = False,
    ) -> None:
        """Replace the stack's environment and redeploy it (Portainer only applies a new
        environment by redeploying). Refuses when ``PORTAINER_READ_ONLY=true``."""
        raise AdapterRequestError("changing a stack's environment is not supported here")

    async def set_git_settings(
        self, stack: Stack, *, reference: str, auto_update_interval: str | None
    ) -> None:
        """Change a Git stack's branch / tag and Portainer's own polling (None: off). The
        environment is sent back unchanged."""
        raise AdapterRequestError("changing a stack's Git settings is not supported here")

    async def inspect(self, endpoint_id: int, ref: str) -> dict[str, Any] | None:
        """Docker's own description of a container, environment values masked."""
        raise AdapterRequestError("inspecting a container is not supported here")
