"""A Portainer with sample data, for DRY_RUN and for trying the pages without one."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from orchestrator.adapters.errors import AdapterRequestError
from orchestrator.adapters.portainer.base import (
    Container,
    ContainerDetail,
    ContainerStats,
    Environment,
    ExecResult,
    GitInfo,
    LogLine,
    Mount,
    PortainerAdapter,
    PortainerCheck,
    PortMapping,
    Stack,
    StackCreate,
    StackListing,
)

_NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

_ENVS = [
    Environment(
        id=1,
        name="local",
        type="docker",
        status="up",
        url="unix:///var/run/docker.sock",
        running=4,
        stopped=1,
        healthy=2,
        unhealthy=0,
        stacks=2,
    ),
    Environment(id=2, name="backup-site", type="agent", status="down", url="tcp://10.0.9.5:9001"),
]

_STACKS = [
    Stack(
        id=11,
        name="dev-xymbols",
        endpoint_id=1,
        type="compose",
        status="active",
        git=GitInfo(
            url="https://gitlab.example.com/xymbols/site.git",
            reference="refs/heads/dev",
            path="docker-compose.yml",
            hash="9f3c2a1d7b6e",
            has_credentials=True,
        ),
        auto_update=True,
        has_webhook=True,
        env_names=("DB_PASSWORD", "APP_KEY", "MAIL_DSN"),
        created_at=(_NOW - timedelta(days=60)).isoformat(),
        updated_at=(_NOW - timedelta(hours=3)).isoformat(),
        created_by="admin",
        updated_by="webhook",
    ),
    Stack(
        id=12,
        name="media",
        endpoint_id=1,
        type="compose",
        status="active",
        git=None,
        env_names=("PUID", "PGID"),
        created_at=(_NOW - timedelta(days=200)).isoformat(),
        updated_at=(_NOW - timedelta(days=12)).isoformat(),
        created_by="admin",
    ),
    Stack(
        id=None, name="legacy-tools", endpoint_id=1, type="compose", status="active", managed=False
    ),
]

_KINDS = {"dev-xymbols": "build", "media": "image"}
_ENV: dict[str, list[tuple[str, str]]] = {
    "dev-xymbols": [
        ("DB_PASSWORD", "sample-db-password"),
        ("APP_KEY", "base64:sample-app-key"),
        ("MAIL_DSN", "smtp://mail.example.lan:25"),
    ],
    "media": [("PUID", "1000"), ("PGID", "1000")],
}
_BASE_HEALTH: dict[str, str] = {}


def _c(
    cid: str,
    name: str,
    state: str,
    health: str,
    image: str,
    stack: str,
    service: str,
    ports: tuple[PortMapping, ...] = (),
) -> Container:
    return Container(
        id=cid,
        name=name,
        state=state,
        status_text=(
            f"Up 3 hours ({health})"
            if health
            else ("Up 3 hours" if state == "running" else "Exited (0) 2 days ago")
        ),
        health=health,
        image=image,
        image_id="sha256:" + cid * 4,
        created_at=(_NOW - timedelta(hours=3)).isoformat(),
        ports=ports,
        stack=stack,
        service=service,
    )


_CONTAINERS = [
    _c(
        "a1b2c3d4e5f6",
        "dev-xymbols-web-1",
        "running",
        "healthy",
        "dev-xymbols-web",
        "dev-xymbols",
        "web",
        (PortMapping(80, "tcp", 8081, "0.0.0.0"),),
    ),
    _c(
        "b2c3d4e5f6a1",
        "dev-xymbols-db-1",
        "running",
        "healthy",
        "postgres:16",
        "dev-xymbols",
        "db",
        (PortMapping(5432, "tcp"),),
    ),
    _c(
        "c3d4e5f6a1b2",
        "media-jellyfin-1",
        "running",
        "",
        "jellyfin/jellyfin:latest",
        "media",
        "jellyfin",
        (PortMapping(8096, "tcp", 8096, "0.0.0.0"),),
    ),
    _c("d4e5f6a1b2c3", "media-sonarr-1", "exited", "", "linuxserver/sonarr", "media", "sonarr"),
    _c("e5f6a1b2c3d4", "legacy-tools-cron-1", "running", "", "alpine:3.20", "legacy-tools", "cron"),
]


_BASE_HEALTH.update({c.id: c.health for c in _CONTAINERS})


class DryRunPortainerAdapter(PortainerAdapter):
    """Per-instance copies of the sample data, so start / stop / redeploy / create /
    delete can be tried (and tested) without touching anything real."""

    def __init__(self, settings: Any = None) -> None:
        self._stacks: list[Stack] = list(_STACKS)
        self._containers: list[Container] = list(_CONTAINERS)
        self._read_only = bool(getattr(settings, "portainer_read_only", False))
        self.calls: list[str] = []
        #: Each stack's environment (values), by stack name.
        self.env: dict[str, list[tuple[str, str]]] = {k: list(v) for k, v in _ENV.items()}
        #: What the next exec writes / how it ends (tests set these).
        self.exec_output: bytes | None = None
        self.exec_exit: int | None = 0
        self.exec_stderr = ""

    @property
    def configured(self) -> bool:
        return True

    @property
    def base_url(self) -> str:
        return "https://portainer.example.lan"

    def _writable(self) -> None:
        if self._read_only:
            raise AdapterRequestError(
                "Portainer is read-only here (PORTAINER_READ_ONLY=true): nothing is changed."
            )

    async def check(self) -> PortainerCheck:
        return PortainerCheck(
            True,
            True,
            True,
            "2.21.4",
            "CE",
            len(_ENVS),
            "Portainer 2.21.4 CE, 2 environment(s) (sample data)",
        )

    async def list_environments(self) -> list[Environment]:
        return list(_ENVS)

    async def list_stacks(self, endpoint_id: int | None = None) -> StackListing:
        stacks = sorted(
            (s for s in self._stacks if endpoint_id is None or s.endpoint_id == endpoint_id),
            key=lambda s: (s.endpoint_id, s.name.lower()),
        )
        listing = StackListing(stacks=stacks)
        if endpoint_id in (None, 2):
            listing.errors[2] = "the environment is down (sample data)"
        return listing

    async def get_stack(self, endpoint_id: int, name: str) -> Stack | None:
        return next(
            (
                s
                for s in self._stacks
                if s.endpoint_id == endpoint_id and s.name.lower() == name.lower()
            ),
            None,
        )

    async def stack_kind(self, stack: Stack) -> str:
        return _KINDS.get(stack.name, "unknown")

    # -- writes ----------------------------------------------------------------------------------

    async def stack_env(self, stack: Stack) -> list[tuple[str, str]]:
        if stack.id is None:
            raise AdapterRequestError(f"{stack.name!r} is not a stack Portainer created")
        return list(self.env.get(stack.name, []))

    async def set_stack_env(
        self,
        stack: Stack,
        env: list[tuple[str, str]],
        *,
        pull_image: bool = True,
        prune: bool = False,
    ) -> None:
        self._writable()
        if stack.id is None:
            raise AdapterRequestError(f"{stack.name!r} is not a stack Portainer created")
        self.calls.append(f"env {stack.name}")
        self.env[stack.name] = list(env)
        self._set_stack(
            stack, env_names=tuple(n for n, _ in env), updated_at=datetime.now(UTC).isoformat()
        )
        self._set_containers(stack, "running")

    async def set_git_settings(
        self, stack: Stack, *, reference: str, auto_update_interval: str | None
    ) -> None:
        self._writable()
        if stack.git is None:
            raise AdapterRequestError(f"{stack.name!r} is not deployed from a Git repository")
        self.calls.append(f"git {stack.name}")
        self._set_stack(
            stack,
            git=replace(stack.git, reference=reference),
            auto_update=bool(auto_update_interval),
            auto_update_interval=auto_update_interval or "",
        )

    async def inspect(self, endpoint_id: int, ref: str) -> dict[str, Any] | None:
        c = self._find(ref) if endpoint_id == 1 else None
        if c is None:
            return None
        return {
            "Id": c.id * 5,
            "Name": "/" + c.name,
            "Image": c.image_id,
            "State": {"Status": c.state, "Running": c.state == "running"},
            "Config": {
                "Image": c.image,
                "Env": ["TZ=***", "PATH=***"],
                "Labels": {"com.docker.compose.project": c.stack or ""},
            },
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        }

    def _set_stack(self, stack: Stack, **changes: Any) -> None:
        self._stacks = [
            replace(s, **changes) if s is stack or (s.id, s.name) == (stack.id, stack.name) else s
            for s in self._stacks
        ]

    def _set_containers(self, stack: Stack, state: str) -> None:
        self._containers = [
            (
                replace(
                    c,
                    state=state,
                    health=(_BASE_HEALTH.get(c.id, "") if state == "running" else ""),
                    status_text="Up 1 second" if state == "running" else "Exited (0) 1 second ago",
                )
                if (c.stack or "").lower() == stack.name.lower()
                else c
            )
            for c in self._containers
        ]

    async def start_stack(self, stack: Stack) -> None:
        self._writable()
        self.calls.append(f"start {stack.name}")
        self._set_stack(stack, status="active")
        self._set_containers(stack, "running")

    async def stop_stack(self, stack: Stack) -> None:
        self._writable()
        self.calls.append(f"stop {stack.name}")
        self._set_stack(stack, status="inactive")
        self._set_containers(stack, "exited")

    async def redeploy_stack(
        self, stack: Stack, *, pull_image: bool = True, prune: bool = False
    ) -> None:
        self._writable()
        if stack.git is None:
            raise AdapterRequestError(f"{stack.name!r} was not deployed from a Git repository")
        self.calls.append(f"redeploy {stack.name}")
        now = datetime.now(UTC)
        new_hash = f"{abs(hash((stack.name, now.timestamp()))) % 16**12:012x}"
        self._set_stack(
            stack,
            updated_at=now.isoformat(),
            status="active",
            git=replace(stack.git, hash=new_hash),
        )
        self._set_containers(stack, "running")

    def preview_create(self, spec: StackCreate) -> dict[str, Any]:
        if spec.source == "string" and not spec.compose_text.strip():
            raise AdapterRequestError("a stack from text needs the compose text")
        if spec.source == "git" and not spec.repository_url.strip():
            raise AdapterRequestError("a stack from Git needs the repository URL")
        body: dict[str, Any] = {
            "name": spec.name,
            "env": [{"name": n, "value": "***"} for n, _ in spec.env],
        }
        if spec.source == "git":
            body.update(
                repositoryURL=spec.repository_url,
                repositoryReferenceName=spec.reference,
                composeFile=spec.compose_path,
            )
        else:
            body["stackFileContent"] = "<compose text>"
        return {
            "method": "POST",
            "path": "/api/stacks/create/standalone/"
            + ("repository" if spec.source == "git" else "string"),
            "params": {"endpointId": spec.endpoint_id},
            "body": body,
        }

    async def create_stack(self, spec: StackCreate) -> Stack:
        self._writable()
        if await self.get_stack(spec.endpoint_id, spec.name) is not None:
            raise AdapterRequestError(
                f"a stack named {spec.name!r} already exists on environment {spec.endpoint_id}"
            )
        self.calls.append(f"create {spec.name}")
        stack = Stack(
            id=max([s.id or 0 for s in self._stacks] + [0]) + 1,
            name=spec.name,
            endpoint_id=spec.endpoint_id,
            type="compose",
            status="active",
            git=(
                GitInfo(spec.repository_url, spec.reference, spec.compose_path, "0" * 12)
                if spec.source == "git"
                else None
            ),
            env_names=tuple(n for n, _ in spec.env),
            created_at=_NOW.isoformat(),
            updated_at=_NOW.isoformat(),
            created_by="orchestrator",
        )
        self._stacks.append(stack)
        return stack

    async def delete_stack(self, stack: Stack) -> None:
        self._writable()
        self.calls.append(f"delete {stack.name}")
        self._stacks = [s for s in self._stacks if (s.id, s.name) != (stack.id, stack.name)]
        self._containers = [
            c for c in self._containers if (c.stack or "").lower() != stack.name.lower()
        ]

    # -- containers ------------------------------------------------------------------------------

    async def exec_stream(
        self,
        endpoint_id: int,
        ref: str,
        cmd: list[str],
        sink: Callable[[bytes], None],
        *,
        timeout_s: float = 1800.0,
    ) -> ExecResult:
        self._writable()
        c = self._find(ref) if endpoint_id == 1 else None
        if c is None:
            raise AdapterRequestError(
                f"no container {ref!r} on Portainer environment {endpoint_id}"
            )
        if c.state != "running":
            raise AdapterRequestError(f"container {c.name} is not running")
        self.calls.append(f"exec {c.name}: {' '.join(cmd)[:120]}")
        data = (
            self.exec_output
            if self.exec_output is not None
            else (
                (
                    f"-- dry-run dump of {c.name}\nSELECT 1;\nCOMMIT;\n"
                    "-- PostgreSQL database dump complete\n-- Dump completed on 2026-10-25\n"
                ).encode()
            )
        )
        sink(data)
        return ExecResult(self.exec_exit, self.exec_stderr, len(data))

    async def container_action(self, endpoint_id: int, ref: str, verb: str) -> str:
        if verb not in {"start", "stop", "restart", "kill", "recreate"}:
            raise AdapterRequestError(f"unknown container action {verb!r}")
        self._writable()
        c = self._find(ref) if endpoint_id == 1 else None
        if c is None:
            raise AdapterRequestError(
                f"no container {ref!r} on Portainer environment {endpoint_id}"
            )
        self.calls.append(f"{verb} container {c.name}")
        up = verb not in {"stop", "kill"}
        self._containers = [
            (
                replace(
                    x,
                    state="running" if up else "exited",
                    health=_BASE_HEALTH.get(x.id, "") if up else "",
                    status_text="Up 1 second" if up else "Exited (0) 1 second ago",
                )
                if x.id == c.id
                else x
            )
            for x in self._containers
        ]
        return f"{verb} sent to container {c.name}"

    async def list_containers(
        self, endpoint_id: int, *, stack: str | None = None, include_stopped: bool = True
    ) -> list[Container]:
        if endpoint_id != 1:
            return []
        out = [
            c for c in self._containers if stack is None or (c.stack or "").lower() == stack.lower()
        ]
        return sorted(
            (c for c in out if include_stopped or c.state == "running"), key=lambda c: c.name
        )

    def _find(self, ref: str) -> Container | None:
        return next((c for c in self._containers if c.name == ref or c.id.startswith(ref)), None)

    async def get_container(self, endpoint_id: int, ref: str) -> ContainerDetail | None:
        c = self._find(ref) if endpoint_id == 1 else None
        if c is None:
            return None
        return ContainerDetail(
            container=c,
            restart_count=0,
            started_at=(_NOW - timedelta(hours=3)).isoformat(),
            restart_policy="unless-stopped",
            health_log=("ok",) if c.health else (),
            mounts=(Mount("volume", f"{c.stack}_data", "/data"),),
            networks=(f"{c.stack}_default",),
            env_names=("TZ", "PATH"),
        )

    async def logs(
        self,
        endpoint_id: int,
        ref: str,
        *,
        tail: int = 200,
        since: int | None = None,
        timestamps: bool = True,
    ) -> list[LogLine]:
        c = self._find(ref) if endpoint_id == 1 else None
        if c is None:
            raise AdapterRequestError(
                f"no container {ref!r} on environment {endpoint_id}", status_code=404
            )
        lines = [
            LogLine(
                ts=(
                    (_NOW - timedelta(seconds=300 - i * 10)).isoformat().replace("+00:00", "Z")
                    if timestamps
                    else None
                ),
                stream="stderr" if i % 9 == 8 else "stdout",
                text=f"[{c.service}] sample log line {i}",
            )
            for i in range(30)
        ]
        return lines[-max(1, tail) :]

    async def stats(self, endpoint_id: int, ref: str) -> ContainerStats:
        if self._find(ref) is None:
            raise AdapterRequestError(
                f"no container {ref!r} on environment {endpoint_id}", status_code=404
            )
        return ContainerStats(
            cpu_percent=1.3,
            mem_bytes=180_000_000,
            mem_limit=2_000_000_000,
            net_rx_bytes=1_200_000,
            net_tx_bytes=800_000,
            pids=14,
        )
