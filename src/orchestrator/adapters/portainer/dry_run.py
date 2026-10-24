"""A Portainer with sample data, for DRY_RUN and for trying the pages without one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from orchestrator.adapters.errors import AdapterRequestError
from orchestrator.adapters.portainer.base import (
    Container,
    ContainerDetail,
    ContainerStats,
    Environment,
    GitInfo,
    LogLine,
    Mount,
    PortainerAdapter,
    PortainerCheck,
    PortMapping,
    Stack,
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


class DryRunPortainerAdapter(PortainerAdapter):
    @property
    def configured(self) -> bool:
        return True

    @property
    def base_url(self) -> str:
        return "https://portainer.example.lan"

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
            (s for s in _STACKS if endpoint_id is None or s.endpoint_id == endpoint_id),
            key=lambda s: (s.endpoint_id, s.name.lower()),
        )
        listing = StackListing(stacks=stacks)
        if endpoint_id in (None, 2):
            listing.errors[2] = "the environment is down (sample data)"
        return listing

    async def get_stack(self, endpoint_id: int, name: str) -> Stack | None:
        return next(
            (s for s in _STACKS if s.endpoint_id == endpoint_id and s.name.lower() == name.lower()),
            None,
        )

    async def stack_kind(self, stack: Stack) -> str:
        return _KINDS.get(stack.name, "unknown")

    async def list_containers(
        self, endpoint_id: int, *, stack: str | None = None, include_stopped: bool = True
    ) -> list[Container]:
        if endpoint_id != 1:
            return []
        out = [c for c in _CONTAINERS if stack is None or (c.stack or "").lower() == stack.lower()]
        return sorted(
            (c for c in out if include_stopped or c.state == "running"), key=lambda c: c.name
        )

    def _find(self, ref: str) -> Container | None:
        return next((c for c in _CONTAINERS if c.name == ref or c.id.startswith(ref)), None)

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
