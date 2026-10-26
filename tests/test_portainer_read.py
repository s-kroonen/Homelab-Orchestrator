"""Portainer, read side: parsing and redaction, the live client against a fake Portainer,
the API, the page, the CLI and the connectivity check."""

from __future__ import annotations

import json
import struct
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import Session

from orchestrator import config as config_module
from orchestrator.adapters.errors import (
    AdapterAuthError,
    AdapterNotConfigured,
    AdapterRequestError,
    AdapterUnreachable,
)
from orchestrator.adapters.portainer import parse
from orchestrator.adapters.portainer.api import LivePortainerAdapter
from orchestrator.cli import cli
from orchestrator.config import get_settings
from orchestrator.db import session as session_module
from orchestrator.db.models import Service
from orchestrator.db.session import get_engine
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin

# Everything below that must never reach a browser, a log or the CLI.
SECRETS = [
    "SUPERSECRET-DB",
    "gitpass-123",
    "tok3n-in-url",
    "wh00k-uuid-abc",
    "api-key-value",
    "ENVVAL-xyz",
]

STACK_RAW = {
    "Id": 11,
    "Name": "dev-xymbols",
    "Type": 2,
    "EndpointId": 1,
    "Status": 1,
    "CreationDate": 1_790_000_000,
    "UpdateDate": 1_790_100_000,
    "CreatedBy": "admin",
    "UpdatedBy": "webhook",
    "Env": [{"name": "DB_PASSWORD", "value": SECRETS[0]}, {"name": "APP_KEY", "value": "k"}],
    "GitConfig": {
        "URL": f"https://oauth2:{SECRETS[2]}@gitlab.example.com/x/site.git",
        "ReferenceName": "refs/heads/dev",
        "ConfigFilePath": "docker-compose.yml",
        "ConfigHash": "9f3c2a1d7b6e",
        "Authentication": {"Username": "u", "Password": SECRETS[1]},
    },
    "AutoUpdate": {"Interval": "", "Webhook": SECRETS[3]},
}
COMPOSE_BUILD = "services:\n  web:\n    build: .\n  db:\n    image: postgres:16\n"
COMPOSE_BUILD_ONLY = "services:\n  web:\n    build: {context: .}\n"
COMPOSE_IMAGE = "services:\n  a:\n    image: x\n  b:\n    image: y\n"


def _ct(
    cid: str,
    name: str,
    project: str | None,
    state: str = "running",
    status: str = "Up 3 hours (healthy)",
) -> dict[str, Any]:
    labels = (
        {"com.docker.compose.project": project, "com.docker.compose.service": name.split("-")[1]}
        if project
        else {}
    )
    return {
        "Id": cid * 8,
        "Names": [f"/{name}"],
        "State": state,
        "Status": status,
        "Image": "img",
        "ImageID": "sha256:abc",
        "Created": 1_790_000_000,
        "Labels": labels,
        "Ports": [{"PrivatePort": 80, "PublicPort": 8081, "Type": "tcp", "IP": "0.0.0.0"}],
    }


CONTAINERS = [
    _ct("a1", "dev-web-1", "dev-xymbols"),
    _ct("b2", "dev-db-1", "dev-xymbols", status="Up 3 hours"),
    _ct("c3", "old-cron-1", "legacy-tools"),
    _ct("d4", "loner", None, state="exited", status="Exited (0) 2 days ago"),
]
INSPECT = {
    "Id": "a1" * 8,
    "Name": "/dev-web-1",
    "Created": "2026-10-01T10:00:00.1Z",
    "Image": "sha256:abc",
    "RestartCount": 2,
    "State": {
        "Status": "running",
        "StartedAt": "2026-10-24T09:00:00Z",
        "FinishedAt": "0001-01-01T00:00:00Z",
        "ExitCode": 0,
        "Health": {"Status": "healthy", "Log": [{"Output": "ok\n"}]},
    },
    "Config": {
        "Image": "dev-xymbols-web",
        "Tty": False,
        "Env": [f"SECRET_TOKEN={SECRETS[5]}", "TZ=UTC"],
        "Labels": {
            "com.docker.compose.project": "dev-xymbols",
            "com.docker.compose.service": "web",
        },
    },
    "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
    "NetworkSettings": {
        "Networks": {"dev_default": {}},
        "Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8081"}], "9000/tcp": None},
    },
    "Mounts": [{"Type": "volume", "Name": "dev_data", "Destination": "/data", "RW": True}],
}


def frame(kind: int, text: str) -> bytes:
    payload = text.encode()
    return bytes([kind, 0, 0, 0]) + struct.pack(">I", len(payload)) + payload


LOGS = frame(1, "2026-10-24T12:00:00.123456789Z hello\n2026-10-24T12:00:01Z second\n") + frame(
    2, "2026-10-24T12:00:02.5Z boom\n"
)


class FakePortainer:
    """Answers the handful of Portainer / Docker-proxy calls the adapter makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.compose = {11: COMPOSE_BUILD}
        self.extra_stacks: list[dict[str, Any]] = []
        self.fail: dict[str, httpx.Response | Exception] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        assert request.headers.get("x-api-key") == "api-key-value"
        for prefix, outcome in self.fail.items():
            if path.startswith(prefix):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        if path == "/api/system/status":
            return httpx.Response(200, json={"Version": "2.21.4", "InstanceID": "x"})
        if path == "/api/system/version":
            return httpx.Response(200, json={"ServerVersion": "2.21.4", "ServerEdition": "CE"})
        if path == "/api/endpoints":
            return httpx.Response(
                200,
                json=[
                    {
                        "Id": 1,
                        "Name": "local",
                        "Type": 1,
                        "Status": 1,
                        "URL": "unix:///var/run/docker.sock",
                        "Snapshots": [
                            {
                                "RunningContainerCount": 3,
                                "StoppedContainerCount": 1,
                                "StackCount": 1,
                            }
                        ],
                    },
                    {"Id": 2, "Name": "k8s", "Type": 5, "Status": 1},
                    {"Id": 3, "Name": "away", "Type": 2, "Status": 2},
                ],
            )
        if path == "/api/stacks":
            return httpx.Response(200, json=[STACK_RAW, *self.extra_stacks])
        if path.startswith("/api/stacks/") and path.endswith("/file"):
            sid = int(path.split("/")[3])
            return httpx.Response(200, json={"StackFileContent": self.compose.get(sid, "")})
        if path == "/api/endpoints/1/docker/containers/json":
            return httpx.Response(200, json=CONTAINERS)
        if path == f"/api/endpoints/1/docker/containers/{'a1' * 8}/json":
            return httpx.Response(200, json=INSPECT)
        if path == f"/api/endpoints/1/docker/containers/{'a1' * 8}/logs":
            return httpx.Response(200, content=LOGS)
        if path == f"/api/endpoints/1/docker/containers/{'a1' * 8}/stats":
            return httpx.Response(
                200,
                json={
                    "cpu_stats": {
                        "cpu_usage": {"total_usage": 300},
                        "system_cpu_usage": 2000,
                        "online_cpus": 2,
                    },
                    "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
                    "memory_stats": {"usage": 500, "limit": 4000, "stats": {"inactive_file": 100}},
                    "networks": {"eth0": {"rx_bytes": 10, "tx_bytes": 20}},
                    "pids_stats": {"current": 7},
                },
            )
        return httpx.Response(404, text="not found")


@pytest.fixture
def fake() -> FakePortainer:
    return FakePortainer()


@pytest.fixture
def live(fake: FakePortainer, monkeypatch: pytest.MonkeyPatch) -> LivePortainerAdapter:
    monkeypatch.setenv("PORTAINER_URL", "https://portainer.test:9443")
    monkeypatch.setenv("PORTAINER_API_KEY", "api-key-value")
    monkeypatch.setenv("PORTAINER_PUBLIC_URL", "https://portainer.example.com")
    config_module.reset_settings_cache()
    return LivePortainerAdapter(get_settings(), transport=httpx.MockTransport(fake))


# -- pure parsing -------------------------------------------------------------------------------


def test_urls_lose_their_credentials() -> None:
    assert parse.redact_url("https://u:tok@git.example.com:8443/a/b.git") == (
        "https://git.example.com:8443/a/b.git",
        True,
    )
    assert parse.redact_url("https://git.example.com/a/b.git") == (
        "https://git.example.com/a/b.git",
        False,
    )


def test_a_stack_keeps_variable_names_and_drops_every_secret() -> None:
    s = parse.stack(STACK_RAW)
    assert s.env_names == ("DB_PASSWORD", "APP_KEY")
    assert s.git is not None and s.git.url == "https://gitlab.example.com/x/site.git"
    assert s.git.has_credentials and s.has_webhook and s.auto_update
    blob = json.dumps(s, default=lambda o: o.__dict__)
    assert not any(secret in blob for secret in SECRETS)


def test_container_detail_keeps_env_names_only() -> None:
    d = parse.container_detail(INSPECT)
    assert d.env_names == ("SECRET_TOKEN", "TZ")
    assert (
        d.restart_count == 2
        and d.container.health == "healthy"
        and d.container.stack == "dev-xymbols"
    )
    assert d.finished_at is None  # Docker's "never" is not a date
    assert [(p.private, p.public) for p in d.container.ports] == [(80, 8081), (9000, None)]
    assert SECRETS[5] not in json.dumps(d, default=lambda o: o.__dict__)


def test_health_comes_from_the_status_text_in_lists() -> None:
    assert parse.container(_ct("a", "x-y-1", "p", status="Up 1 hour (healthy)")).health == "healthy"
    assert (
        parse.container(_ct("a", "x-y-1", "p", status="Up 1 hour (unhealthy)")).health
        == "unhealthy"
    )
    assert (
        parse.container(_ct("a", "x-y-1", "p", status="Up 5 seconds (health: starting)")).health
        == "starting"
    )
    assert parse.container(_ct("a", "x-y-1", "p", status="Up 1 hour")).health == ""


def test_the_multiplexed_log_stream_is_decoded_with_streams_and_timestamps() -> None:
    lines = parse.parse_log_stream(LOGS, tty=False)
    assert [(line.stream, line.text) for line in lines] == [
        ("stdout", "hello"),
        ("stdout", "second"),
        ("stderr", "boom"),
    ]
    assert lines[0].ts == "2026-10-24T12:00:00.123456789Z"


def test_logs_of_a_tty_container_and_odd_bodies_are_read_as_text() -> None:
    assert [
        line.text for line in parse.parse_log_stream(b"one\ntwo\n", tty=True, timestamps=False)
    ] == ["one", "two"]
    # not frames at all (wrong header): still readable, never lost
    assert [
        line.text
        for line in parse.parse_log_stream(b"plain text line\n", tty=False, timestamps=False)
    ] == ["plain text line"]
    # a frame cut off mid-payload: what is there is shown
    cut = frame(1, "abcdef\n")[:-3]
    assert parse.parse_log_stream(cut, tty=False, timestamps=False)[0].text.startswith("abc")
    assert parse.parse_log_stream(b"", tty=False) == []


def test_nanosecond_timestamps_become_unix_seconds() -> None:
    assert parse.epoch_from_iso("2026-10-24T12:00:00.123456789Z") == parse.epoch_from_iso(
        "2026-10-24T12:00:00Z"
    )
    assert parse.epoch_from_iso("2026-10-24T14:00:00+02:00") == parse.epoch_from_iso(
        "2026-10-24T12:00:00Z"
    )
    assert parse.epoch_from_iso("garbage") is None and parse.epoch_from_iso(None) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (COMPOSE_IMAGE, "image"),
        (COMPOSE_BUILD_ONLY, "build"),
        (COMPOSE_BUILD, "mixed"),
        ("services: {}\n", "unknown"),
        ("{ not yaml [", "unknown"),
        ("", "unknown"),
    ],
)
def test_a_compose_file_is_labelled_image_build_or_mixed(text: str, expected: str) -> None:
    assert parse.classify_compose(text) == expected


# -- the live client ------------------------------------------------------------------------------


async def test_check_reports_version_edition_and_environment_count(
    live: LivePortainerAdapter,
) -> None:
    check = await live.check()
    assert (
        check.ok and check.version == "2.21.4" and check.edition == "CE" and check.environments == 3
    )
    assert "2.21.4" in check.detail and live.base_url == "https://portainer.example.com"


async def test_an_unconfigured_portainer_says_what_to_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTAINER_URL", "")
    monkeypatch.setenv("PORTAINER_API_KEY", "")
    config_module.reset_settings_cache()
    adapter = LivePortainerAdapter(get_settings())
    check = await adapter.check()
    assert not check.configured and "PORTAINER_URL" in check.detail
    with pytest.raises(AdapterNotConfigured):
        await adapter.list_environments()


async def test_environments_are_typed_and_carry_the_snapshot_counts(
    live: LivePortainerAdapter,
) -> None:
    envs = await live.list_environments()
    assert [(e.id, e.type, e.status) for e in envs] == [
        (1, "docker", "up"),
        (2, "kubernetes", "up"),
        (3, "agent", "down"),
    ]
    assert (
        envs[0].running == 3 and envs[0].stacks == 1 and envs[0].is_docker and not envs[1].is_docker
    )


async def test_stacks_include_compose_projects_portainer_did_not_create(
    live: LivePortainerAdapter,
) -> None:
    listing = await live.list_stacks()
    by_name = {s.name: s for s in listing.stacks}
    assert set(by_name) == {
        "dev-xymbols",
        "legacy-tools",
    }  # k8s and the down environment are skipped
    assert by_name["dev-xymbols"].managed and by_name["dev-xymbols"].id == 11
    assert not by_name["legacy-tools"].managed and by_name["legacy-tools"].status == "active"
    assert listing.errors == {}
    assert (await live.get_stack(1, "DEV-XYMBOLS")) is not None and await live.get_stack(
        1, "nope"
    ) is None


async def test_stack_kind_reads_the_compose_file_and_keeps_nothing(
    live: LivePortainerAdapter, fake: FakePortainer
) -> None:
    stack = await live.get_stack(1, "dev-xymbols")
    assert stack is not None
    assert await live.stack_kind(stack) == "mixed"
    fake.compose[11] = COMPOSE_IMAGE
    live.invalidate()
    assert await live.stack_kind(stack) == "image"
    external = await live.get_stack(1, "legacy-tools")
    assert external is not None and await live.stack_kind(external) == "unknown"


async def test_containers_filter_by_stack_and_state(live: LivePortainerAdapter) -> None:
    everything = await live.list_containers(1)
    assert [c.name for c in everything] == ["dev-db-1", "dev-web-1", "loner", "old-cron-1"]
    assert [c.name for c in await live.list_containers(1, stack="DEV-xymbols")] == [
        "dev-db-1",
        "dev-web-1",
    ]
    assert [c.name for c in await live.list_containers(1, include_stopped=False)] == [
        "dev-db-1",
        "dev-web-1",
        "old-cron-1",
    ]


async def test_a_container_is_found_by_name_or_id_prefix(live: LivePortainerAdapter) -> None:
    by_name = await live.get_container(1, "dev-web-1")
    by_id = await live.get_container(1, "a1a1a1")
    assert by_name is not None and by_id is not None and by_name.container.id == by_id.container.id
    assert await live.get_container(1, "ghost") is None


async def test_logs_are_decoded_and_the_tail_is_bounded(
    live: LivePortainerAdapter, fake: FakePortainer
) -> None:
    lines = await live.logs(1, "dev-web-1", tail=99999)
    assert [(x.stream, x.text) for x in lines][-1] == ("stderr", "boom")
    log_call = [c for c in fake.calls if c[1].endswith("/logs")]
    assert log_call
    with pytest.raises(AdapterRequestError):
        await live.logs(1, "ghost")


async def test_stats_are_computed_from_the_deltas(live: LivePortainerAdapter) -> None:
    s = await live.stats(1, "dev-web-1")
    assert s.cpu_percent == 40.0  # (200/1000) * 2 cpus * 100
    assert s.mem_bytes == 400 and s.mem_limit == 4000 and s.net_rx_bytes == 10 and s.pids == 7


async def test_repeated_reads_are_served_from_memory_for_a_moment(
    live: LivePortainerAdapter, fake: FakePortainer
) -> None:
    await live.list_environments()
    await live.list_environments()
    assert sum(1 for c in fake.calls if c[1] == "/api/endpoints") == 1
    live.invalidate()
    await live.list_environments()
    assert sum(1 for c in fake.calls if c[1] == "/api/endpoints") == 2


async def test_errors_are_told_apart(live: LivePortainerAdapter, fake: FakePortainer) -> None:
    fake.fail["/api/endpoints"] = httpx.Response(401, text="bad key")
    with pytest.raises(AdapterAuthError, match="refused the API key"):
        await live.list_environments()
    check = await live.check()
    assert check.reachable and not check.ok and "refused" in check.detail

    fake.fail["/api/endpoints"] = httpx.ConnectError("no route")
    live.invalidate()
    with pytest.raises(AdapterUnreachable):
        await live.list_environments()
    assert not (await live.check()).reachable

    fake.fail["/api/endpoints"] = httpx.Response(500, text="boom")
    with pytest.raises(AdapterRequestError, match="500"):
        await live.list_environments()


async def test_one_failing_environment_does_not_hide_the_others(
    live: LivePortainerAdapter, fake: FakePortainer
) -> None:
    fake.fail["/api/endpoints/1/docker/containers/json"] = httpx.Response(500, text="docker down")
    listing = await live.list_stacks()
    assert [s.name for s in listing.stacks] == ["dev-xymbols"]  # the managed one still shows
    assert "500" in listing.errors[1]


async def test_a_proxy_error_page_is_described_not_dumped(
    live: LivePortainerAdapter, fake: FakePortainer
) -> None:
    page = (
        "<!DOCTYPE html><html><head><title>portainer \u2014 portainer is not available</title>"
        "<style>body{}</style></head><body>x</body></html>"
    )
    fake.fail["/api/endpoints/1/docker/containers/json"] = httpx.Response(502, text=page)
    with pytest.raises(AdapterRequestError) as caught:
        await live.list_containers(1)
    text = str(caught.value)
    assert "502" in text and "reverse proxy" in text and "PORTAINER_URL" in text
    assert "<style>" not in text and "portainer is not available" in text


# -- the API, the page, the CLI -------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    session_module.create_all_for_tests()  # DRY_RUN=true: the sample Portainer answers
    with TestClient(create_app()) as c:
        _login_as_first_admin(c, monkeypatch)
        yield c


def test_status_and_environments(client: TestClient) -> None:
    status = client.get("/api/portainer/status").json()
    assert status["ok"] and status["version"] == "2.21.4" and status["url"].startswith("https://")
    envs = client.get("/api/portainer/environments").json()
    assert [e["name"] for e in envs] == ["local", "backup-site"]
    assert envs[0]["open_url"].endswith("/#!/1/docker/dashboard")


def test_stacks_show_kind_container_counts_and_what_is_linked(client: TestClient) -> None:
    with Session(get_engine()) as s:
        s.add(
            Service(
                slug="dev-xymbols",
                name="dev.xymbols",
                controller={"type": "portainer", "endpoint": 1, "stack": "dev-xymbols"},
            )
        )
        s.add(
            Service(
                slug="dev-db",
                name="db",
                controller={"type": "portainer", "endpoint": 1, "container": "dev-xymbols-db-1"},
            )
        )
        s.commit()

    body = client.get("/api/portainer/stacks").json()
    rows = {r["name"]: r for r in body["stacks"]}
    assert rows["dev-xymbols"]["kind"] == "build" and rows["media"]["kind"] == "image"
    assert (
        rows["dev-xymbols"]["containers_total"] == 2
        and rows["dev-xymbols"]["containers_running"] == 2
    )
    assert rows["media"]["containers_running"] == 1 and rows["media"]["containers_total"] == 2
    assert (
        rows["dev-xymbols"]["linked_services"] == ["dev-xymbols"]
        and rows["media"]["linked_services"] == []
    )
    assert rows["legacy-tools"]["managed"] is False
    assert rows["dev-xymbols"]["open_url"].endswith(
        "/#!/1/docker/stacks/dev-xymbols?id=11&type=2&regular=true"
    )
    assert "docker compose -p dev-xymbols ps" in rows["dev-xymbols"]["host_commands"]
    assert "2" in body["errors"]  # the environment that is down is reported, not hidden
    assert [r["name"] for r in client.get("/api/portainer/stacks?endpoint=1").json()["stacks"]] == [
        "dev-xymbols",
        "legacy-tools",
        "media",
    ]


def test_stack_detail_lists_its_containers_and_their_links(client: TestClient) -> None:
    with Session(get_engine()) as s:
        s.add(
            Service(
                slug="dev-db",
                name="db",
                controller={"type": "portainer", "endpoint": 1, "container": "dev-xymbols-db-1"},
            )
        )
        s.commit()
    d = client.get("/api/portainer/endpoints/1/stacks/dev-xymbols").json()
    assert d["env_names"] == ["DB_PASSWORD", "APP_KEY", "MAIL_DSN"]
    assert d["git"]["url"] == "https://gitlab.example.com/xymbols/site.git"
    names = {c["name"]: c for c in d["containers"]}
    assert set(names) == {"dev-xymbols-web-1", "dev-xymbols-db-1"}
    assert names["dev-xymbols-db-1"]["linked_services"] == ["dev-db"]
    assert names["dev-xymbols-web-1"]["open_url"].endswith("/#!/1/docker/containers/a1b2c3d4e5f6")
    assert client.get("/api/portainer/endpoints/1/stacks/ghost").status_code == 404


def test_containers_detail_logs_and_stats(client: TestClient) -> None:
    rows = client.get("/api/portainer/endpoints/1/containers?stack=media").json()
    assert {r["name"] for r in rows} == {"media-jellyfin-1", "media-sonarr-1"}
    assert [
        r["name"] for r in client.get("/api/portainer/endpoints/1/containers?all=false").json()
    ] == [
        "dev-xymbols-db-1",
        "dev-xymbols-web-1",
        "legacy-tools-cron-1",
        "media-jellyfin-1",
    ]
    d = client.get("/api/portainer/endpoints/1/containers/dev-xymbols-web-1").json()
    assert (
        d["env_names"] == ["TZ", "PATH"]
        and "docker logs --tail 200 -f dev-xymbols-web-1" in d["host_commands"]
    )
    logs = client.get("/api/portainer/endpoints/1/containers/dev-xymbols-web-1/logs?tail=5").json()
    assert logs["count"] == 5 and logs["lines"][-1]["text"].endswith("line 29")
    assert (
        client.get("/api/portainer/endpoints/1/containers/dev-xymbols-web-1/stats").json()["pids"]
        == 14
    )
    assert client.get("/api/portainer/endpoints/1/containers/ghost").status_code == 404
    assert client.get("/api/portainer/endpoints/1/containers/ghost/logs").status_code == 404
    assert client.get("/api/portainer/endpoints/1/containers/x/logs?tail=0").status_code == 422


def test_nothing_secret_leaves_the_api_when_the_live_client_is_behind_it(
    client: TestClient, live: LivePortainerAdapter
) -> None:
    client.app.state.adapters.portainer = live  # type: ignore[attr-defined]
    blobs = [
        client.get("/api/portainer/stacks").text,
        client.get("/api/portainer/endpoints/1/stacks/dev-xymbols").text,
        client.get("/api/portainer/endpoints/1/containers/dev-web-1").text,
        client.get("/api/portainer/endpoints/1/containers").text,
        client.get("/api/portainer/status").text,
    ]
    joined = "\n".join(blobs)
    assert "dev-xymbols" in joined and "DB_PASSWORD" in joined  # the names are there ...
    for secret in SECRETS:
        assert secret not in joined, secret  # ... the values never are


def test_the_api_maps_portainer_errors_to_http(
    client: TestClient, live: LivePortainerAdapter, fake: FakePortainer
) -> None:
    client.app.state.adapters.portainer = live  # type: ignore[attr-defined]
    fake.fail["/api/endpoints"] = httpx.Response(401, text="no")
    assert client.get("/api/portainer/environments").status_code == 502
    fake.fail["/api/endpoints"] = httpx.ConnectError("down")
    live.invalidate()
    assert client.get("/api/portainer/environments").status_code == 502
    client.app.state.adapters.portainer = None  # type: ignore[attr-defined]
    assert client.get("/api/portainer/environments").status_code == 503


def test_unconfigured_live_portainer_answers_503_with_the_setting_to_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PORTAINER_URL", "")
    config_module.reset_settings_cache()
    client.app.state.adapters.portainer = LivePortainerAdapter(get_settings())  # type: ignore[attr-defined]
    r = client.get("/api/portainer/stacks")
    assert r.status_code == 503 and "PORTAINER_URL" in r.json()["detail"]


def test_the_api_and_page_need_a_login() -> None:
    session_module.create_all_for_tests()
    with TestClient(create_app()) as anon:
        assert anon.get("/api/portainer/status").status_code == 401
        assert anon.get("/api/portainer/endpoints/1/containers/x/logs").status_code == 401
        r = anon.get("/portainer", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_the_page_renders_and_is_in_the_nav(client: TestClient) -> None:
    r = client.get("/portainer")
    assert r.status_code == 200 and 'id="stack-body"' in r.text and "/static/portainer.js" in r.text
    assert 'href="/portainer"' in r.text
    assert client.get("/static/portainer.js").status_code == 200


def test_connectivity_rows_list_portainer_and_its_environments(client: TestClient) -> None:
    rows = [
        r
        for r in client.get("/api/infra/status").json()["checks"]
        if r["target"].startswith("portainer")
    ]
    by = {r["target"]: r for r in rows}
    assert by["portainer"]["ok"] and "2.21.4" in by["portainer"]["result"]["detail"]
    assert by["portainer:env 1 (local)"]["ok"]
    assert (
        by["portainer:env 2 (backup-site)"]["reachable"] is False
    )  # a down environment is a finding


def test_a_portainer_that_is_not_configured_adds_no_failing_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PORTAINER_URL", "")
    config_module.reset_settings_cache()
    client.app.state.adapters.portainer = LivePortainerAdapter(get_settings())  # type: ignore[attr-defined]
    rows = [
        r
        for r in client.get("/api/infra/status").json()["checks"]
        if r["target"].startswith("portainer")
    ]
    assert rows == []  # optional: nothing to report, nothing red


def test_the_cli_reads_the_same_things() -> None:
    runner = CliRunner()
    out = runner.invoke(cli, ["portainer", "stacks"]).output
    assert (
        "dev-xymbols" in out and "build" in out and "legacy-tools" in out and "environment 2" in out
    )
    assert (
        "dev-xymbols-web-1"
        in runner.invoke(cli, ["portainer", "containers", "-e", "1", "-s", "dev-xymbols"]).output
    )
    logs = runner.invoke(
        cli, ["portainer", "logs", "dev-xymbols-web-1", "-e", "1", "-n", "2"]
    ).output
    assert "sample log line 29" in logs
    status = runner.invoke(cli, ["portainer", "status"])
    assert status.exit_code == 0 and "2.21.4" in status.output
    raw = runner.invoke(cli, ["portainer", "environments", "--json"]).output
    as_json = json.loads(raw[raw.index("[") : raw.rindex("]") + 1])
    assert as_json[0]["name"] == "local"
    missing = runner.invoke(cli, ["portainer", "logs", "ghost", "-e", "1"])
    assert missing.exit_code != 0 and "no container" in missing.output


def test_an_unreadable_environment_does_not_fail_the_stack_list(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = client.app.state.adapters.portainer  # type: ignore[attr-defined]
    real = adapter.list_containers

    async def flaky(endpoint_id: int, **kw: Any) -> Any:
        if endpoint_id == 1:
            raise AdapterRequestError("Portainer answered 502 on GET /x: proxy page")
        return await real(endpoint_id, **kw)

    monkeypatch.setattr(adapter, "list_containers", flaky)
    r = client.get("/api/portainer/stacks")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "unreachable, hidden (Portainer answered 502)" in body["errors"]["1"]
    assert "1" in body["errors"] and 1 in body["skipped"] and "<" not in body["errors"]["1"]
    # its stacks are not listed at all: "active" next to an environment nobody can reach misleads
    assert all(s["endpoint_id"] != 1 for s in body["stacks"])


async def test_stacks_of_a_down_environment_are_not_listed(
    live: LivePortainerAdapter, fake: FakePortainer
) -> None:
    """Portainer keeps a managed stack's record, still 'active', after its environment is down."""
    listing = await live.list_stacks()
    assert all(s.endpoint_id != 3 for s in listing.stacks)
    fake.extra_stacks.append({**STACK_RAW, "Id": 77, "Name": "ghost", "EndpointId": 3})  # 3 is down
    live.invalidate()
    listing = await live.list_stacks()
    assert "ghost" not in {s.name for s in listing.stacks}
    assert "down" in listing.errors[3]
