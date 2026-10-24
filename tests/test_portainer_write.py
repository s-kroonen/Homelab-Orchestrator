"""Portainer, write side: start / stop / redeploy / create / delete - against a fake
Portainer that records exactly what it was asked, plus the dry-run sample."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from orchestrator import config as config_module
from orchestrator.adapters.errors import AdapterAuthError, AdapterRequestError
from orchestrator.adapters.portainer.api import LivePortainerAdapter
from orchestrator.adapters.portainer.base import GitInfo, Stack, StackCreate
from orchestrator.adapters.portainer.dry_run import DryRunPortainerAdapter
from orchestrator.config import get_settings

RAW_STACK = {
    "Id": 11,
    "Name": "dev-xymbols",
    "Type": 2,
    "EndpointId": 1,
    "Status": 1,
    "Env": [{"name": "DB_PASSWORD", "value": "SUPERSECRET-DB"}, {"name": "APP_KEY", "value": "k"}],
    "GitConfig": {
        "URL": "https://gitlab.example.com/x/site.git",
        "ReferenceName": "refs/heads/dev",
        "ConfigFilePath": "docker-compose.yml",
        "ConfigHash": "9f3c",
        "Authentication": {"GitCredentialID": 7, "Username": "", "Password": ""},
    },
}
GIT_STACK = Stack(
    id=11,
    name="dev-xymbols",
    endpoint_id=1,
    type="compose",
    status="active",
    git=GitInfo(
        "https://gitlab.example.com/x/site.git", "refs/heads/dev", "docker-compose.yml", "9f3c"
    ),
)
PLAIN_STACK = Stack(id=12, name="media", endpoint_id=1, type="compose", status="active")
EXTERNAL = Stack(
    id=None, name="legacy", endpoint_id=1, type="compose", status="active", managed=False
)


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: dict[tuple[str, str], httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        hit = self.responses.get((request.method, request.url.path))
        if hit is not None:
            return hit
        if request.method == "GET" and request.url.path == "/api/stacks/11":
            return httpx.Response(200, json=RAW_STACK)
        if request.method == "GET" and request.url.path == "/api/endpoints":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    def last(self) -> httpx.Request:
        return self.requests[-1]

    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET"]


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


def _adapter(rec: Recorder, monkeypatch: pytest.MonkeyPatch, **env: str) -> LivePortainerAdapter:
    monkeypatch.setenv("PORTAINER_URL", "https://portainer.test:9443")
    monkeypatch.setenv("PORTAINER_API_KEY", "key")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config_module.reset_settings_cache()
    return LivePortainerAdapter(get_settings(), transport=httpx.MockTransport(rec))


@pytest.fixture
def live(rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> LivePortainerAdapter:
    return _adapter(rec, monkeypatch)


async def test_start_and_stop_post_to_the_stack(live: LivePortainerAdapter, rec: Recorder) -> None:
    await live.start_stack(GIT_STACK)
    await live.stop_stack(GIT_STACK)
    start, stop = rec.writes()
    assert (start.method, start.url.path, dict(start.url.params)) == (
        "POST",
        "/api/stacks/11/start",
        {"endpointId": "1"},
    )
    assert (stop.method, stop.url.path) == ("POST", "/api/stacks/11/stop")


async def test_a_redeploy_sends_the_current_environment_back_so_it_is_not_wiped(
    live: LivePortainerAdapter, rec: Recorder
) -> None:
    await live.redeploy_stack(GIT_STACK, pull_image=True, prune=True)

    (put,) = rec.writes()
    assert (put.method, put.url.path, dict(put.url.params)) == (
        "PUT",
        "/api/stacks/11/git/redeploy",
        {"endpointId": "1"},
    )
    body = json.loads(put.content)
    assert (
        body["env"] == RAW_STACK["Env"]
    )  # Portainer replaces env with this list: it must be the current one
    assert body["pullImage"] is True and body["prune"] is True
    assert body["repositoryReferenceName"] == "refs/heads/dev"
    assert body["repositoryAuthentication"] is True and body["repositoryGitCredentialID"] == 7


async def test_a_redeploy_refuses_what_has_no_git_source_or_no_id(
    live: LivePortainerAdapter, rec: Recorder
) -> None:
    with pytest.raises(AdapterRequestError, match="Git repository"):
        await live.redeploy_stack(PLAIN_STACK)
    with pytest.raises(AdapterRequestError, match="did not create"):
        await live.redeploy_stack(EXTERNAL)
    with pytest.raises(AdapterRequestError, match="did not create"):
        await live.stop_stack(EXTERNAL)
    assert rec.writes() == []


def test_the_create_preview_masks_every_secret(live: LivePortainerAdapter) -> None:
    spec = StackCreate(
        endpoint_id=1,
        name="app",
        repository_url="https://gitlab.example.com/x/app.git",
        git_username="deploy",
        git_password="gitpass-123",
        env=(("DB_PASSWORD", "SUPERSECRET-DB"),),
    )
    preview = live.preview_create(spec)
    blob = json.dumps(preview)
    assert "SUPERSECRET-DB" not in blob and "gitpass-123" not in blob
    assert preview["path"] == "/api/stacks/create/standalone/repository"
    assert preview["body"]["env"] == [{"name": "DB_PASSWORD", "value": "***"}]
    assert preview["body"]["repositoryPassword"] == "***"
    text = live.preview_create(
        StackCreate(
            endpoint_id=1, name="t", source="string", compose_text="services:\n  a:\n    image: x\n"
        )
    )
    assert "services" not in json.dumps(text) and "line(s)" in text["body"]["stackFileContent"]


async def test_create_from_git_sends_the_real_values_and_returns_a_redacted_stack(
    live: LivePortainerAdapter, rec: Recorder
) -> None:
    rec.responses[("POST", "/api/stacks/create/standalone/repository")] = httpx.Response(
        200, json=RAW_STACK
    )
    spec = StackCreate(
        endpoint_id=1,
        name="dev-xymbols",
        repository_url="https://gitlab.example.com/x/site.git",
        reference="refs/heads/dev",
        git_credential_id=7,
        env=(("DB_PASSWORD", "SUPERSECRET-DB"),),
        auto_update=True,
        auto_update_interval="10m",
    )

    stack = await live.create_stack(spec)

    (post,) = rec.writes()
    body = json.loads(post.content)
    assert dict(post.url.params) == {"endpointId": "1"}
    assert body["env"] == [
        {"name": "DB_PASSWORD", "value": "SUPERSECRET-DB"}
    ]  # sent through, not kept
    assert (
        body["repositoryURL"] == "https://gitlab.example.com/x/site.git"
        and body["repositoryGitCredentialID"] == 7
    )
    assert body["repositoryAuthentication"] is True and "repositoryPassword" not in body
    assert body["autoUpdate"] == {"interval": "10m", "webhook": ""}
    assert stack.id == 11 and stack.env_names == ("DB_PASSWORD", "APP_KEY")
    assert "SUPERSECRET-DB" not in json.dumps(stack, default=lambda o: o.__dict__)


async def test_create_from_text_and_its_validation(
    live: LivePortainerAdapter, rec: Recorder
) -> None:
    await live.create_stack(
        StackCreate(
            endpoint_id=2, name="t", source="string", compose_text="services: {a: {image: x}}"
        )
    )
    (post,) = rec.writes()
    assert post.url.path == "/api/stacks/create/standalone/string" and dict(post.url.params) == {
        "endpointId": "2"
    }
    assert json.loads(post.content)["stackFileContent"].startswith("services")
    with pytest.raises(AdapterRequestError, match="compose text"):
        await live.create_stack(StackCreate(endpoint_id=1, name="x", source="string"))
    with pytest.raises(AdapterRequestError, match="repository URL"):
        await live.create_stack(StackCreate(endpoint_id=1, name="x"))
    with pytest.raises(AdapterRequestError, match="Git repository or from compose text"):
        await live.create_stack(StackCreate(endpoint_id=1, name="x", source="zip"))


async def test_delete_keeps_the_volumes(live: LivePortainerAdapter, rec: Recorder) -> None:
    await live.delete_stack(GIT_STACK)
    (delete,) = rec.writes()
    assert (delete.method, delete.url.path) == ("DELETE", "/api/stacks/11")
    assert dict(delete.url.params) == {
        "endpointId": "1",
        "external": "false",
        "removeVolumes": "false",
    }


async def test_portainers_own_explanation_is_in_the_error(
    live: LivePortainerAdapter, rec: Recorder
) -> None:
    rec.responses[("POST", "/api/stacks/create/standalone/repository")] = httpx.Response(
        409, json={"message": "A stack with this name already exists", "details": "name: dev"}
    )
    with pytest.raises(AdapterRequestError, match="already exists - name: dev") as err:
        await live.create_stack(
            StackCreate(endpoint_id=1, name="dev", repository_url="https://x/y.git")
        )
    assert err.value.status_code == 409


async def test_a_refused_write_says_writing_needs_access(
    live: LivePortainerAdapter, rec: Recorder
) -> None:
    rec.responses[("POST", "/api/stacks/11/stop")] = httpx.Response(
        403, json={"message": "Access denied"}
    )
    with pytest.raises(AdapterAuthError, match="writing needs a user with access"):
        await live.stop_stack(GIT_STACK)


async def test_read_only_mode_refuses_every_write_without_sending_anything(
    rec: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    ro = _adapter(rec, monkeypatch, PORTAINER_READ_ONLY="true")
    for call in (
        ro.start_stack(GIT_STACK),
        ro.stop_stack(GIT_STACK),
        ro.redeploy_stack(GIT_STACK),
        ro.delete_stack(GIT_STACK),
        ro.create_stack(StackCreate(endpoint_id=1, name="x", repository_url="https://x/y.git")),
    ):
        with pytest.raises(AdapterRequestError, match="read-only"):
            await call
    assert rec.requests == []


async def test_a_write_clears_the_read_cache(live: LivePortainerAdapter, rec: Recorder) -> None:
    await live.list_environments()
    await live.list_environments()
    assert sum(1 for r in rec.requests if r.url.path == "/api/endpoints") == 1
    await live.stop_stack(GIT_STACK)
    await live.list_environments()
    assert sum(1 for r in rec.requests if r.url.path == "/api/endpoints") == 2


# -- the dry-run sample ---------------------------------------------------------------------------


@pytest.fixture
def dry() -> DryRunPortainerAdapter:
    return DryRunPortainerAdapter(get_settings())


async def _states(dry: DryRunPortainerAdapter, stack: str) -> set[str]:
    return {c.state for c in await dry.list_containers(1, stack=stack)}


async def test_dry_run_stop_start_changes_the_samples_state(dry: DryRunPortainerAdapter) -> None:
    stack = await dry.get_stack(1, "dev-xymbols")
    assert stack is not None
    await dry.stop_stack(stack)
    again = await dry.get_stack(1, "dev-xymbols")
    assert (
        again is not None
        and again.status == "inactive"
        and await _states(dry, "dev-xymbols") == {"exited"}
    )
    await dry.start_stack(again)
    assert (await dry.get_stack(1, "dev-xymbols")).status == "active"  # type: ignore[union-attr]
    assert await _states(dry, "dev-xymbols") == {"running"}
    assert {c.health for c in await dry.list_containers(1, stack="dev-xymbols")} == {"healthy"}


async def test_dry_run_redeploy_moves_the_git_hash_and_needs_a_git_stack(
    dry: DryRunPortainerAdapter,
) -> None:
    stack = await dry.get_stack(1, "dev-xymbols")
    assert stack is not None and stack.git is not None
    before = stack.git.hash
    await dry.redeploy_stack(stack)
    after = await dry.get_stack(1, "dev-xymbols")
    assert after is not None and after.git is not None and after.git.hash != before
    media = await dry.get_stack(1, "media")
    assert media is not None
    with pytest.raises(AdapterRequestError):
        await dry.redeploy_stack(media)


async def test_dry_run_create_and_delete(dry: DryRunPortainerAdapter) -> None:
    created = await dry.create_stack(
        StackCreate(
            endpoint_id=1, name="fresh", repository_url="https://x/y.git", env=(("A", "1"),)
        )
    )
    assert created.id and created.env_names == ("A",)
    assert (await dry.get_stack(1, "fresh")) is not None
    with pytest.raises(AdapterRequestError, match="already exists"):
        await dry.create_stack(
            StackCreate(endpoint_id=1, name="fresh", repository_url="https://x/y.git")
        )
    await dry.delete_stack(created)
    assert (await dry.get_stack(1, "fresh")) is None


async def test_each_dry_run_adapter_has_its_own_copy(dry: DryRunPortainerAdapter) -> None:
    other = DryRunPortainerAdapter(get_settings())
    stack = await dry.get_stack(1, "media")
    assert stack is not None
    await dry.stop_stack(stack)
    assert (await other.get_stack(1, "media")).status == "active"  # type: ignore[union-attr]


async def test_dry_run_honours_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTAINER_READ_ONLY", "true")
    config_module.reset_settings_cache()
    ro = DryRunPortainerAdapter(get_settings())
    stack = await ro.get_stack(1, "media")
    assert stack is not None
    with pytest.raises(AdapterRequestError, match="read-only"):
        await ro.stop_stack(stack)


def _unused(_: Any) -> None:  # keep Any imported for type comments above
    return None
