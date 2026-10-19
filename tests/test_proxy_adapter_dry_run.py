"""``DryRunProxyAdapter``: never makes a real HTTP call, never raises — for
every method, including the three general route-CRUD ones added for
live-managed proxy hosts (see ``docs/proxy_hosts.md``).
"""

from __future__ import annotations

import httpx
import pytest

from orchestrator.adapters.proxy.dry_run import DryRunProxyAdapter


@pytest.fixture(autouse=True)
def _forbid_real_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if anything here ever tries a real network call."""

    class _ForbiddenAsyncClient(httpx.AsyncClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("DryRunProxyAdapter must never construct a real httpx client")

    monkeypatch.setattr(httpx, "AsyncClient", _ForbiddenAsyncClient)


async def test_create_route_returns_a_fake_ref_and_never_raises() -> None:
    adapter = DryRunProxyAdapter()
    ref = await adapter.create_route(
        hostname="app.example.com", upstream="http://10.0.0.9:80", entry_points=["websecure"]
    )
    assert isinstance(ref, str)
    assert ref  # non-empty, usable as a stand-in ProxyHost.extra value


async def test_update_route_never_raises() -> None:
    adapter = DryRunProxyAdapter()
    # Must not raise — including for a nonsense ref/upstream, since this is
    # a fake backend with nothing to validate against.
    await adapter.update_route(
        "whatever-ref", hostname="app.example.com", upstream="", entry_points=["web"]
    )


async def test_delete_route_never_raises() -> None:
    adapter = DryRunProxyAdapter()
    await adapter.delete_route("whatever-ref")


async def test_create_route_refs_are_stable_but_not_required_to_be_shared() -> None:
    """Each call returns *a* usable ref; this adapter never talks to a real
    backend, so there's no shared state to deduplicate against."""
    adapter = DryRunProxyAdapter()
    ref1 = await adapter.create_route(
        hostname="a.example.com", upstream="http://x:1", entry_points=["web"]
    )
    ref2 = await adapter.create_route(
        hostname="b.example.com", upstream="http://x:1", entry_points=["web"]
    )
    assert ref1 != ref2


async def test_tail_access_log_returns_none_and_never_raises() -> None:
    """Unlike the write methods above, this never fabricates plausible-
    looking log lines — see the adapter's own docstring for why."""
    adapter = DryRunProxyAdapter()
    assert await adapter.tail_access_log() is None
    assert await adapter.tail_access_log(lines=1000) is None


async def test_fetch_access_log_explains_itself_and_runtime_routers_are_empty() -> None:
    adapter = DryRunProxyAdapter()
    tail = await adapter.fetch_access_log(lines=10)
    assert tail.lines is None
    assert tail.error and "Dry-run" in tail.error
    assert await adapter.list_runtime_routers() == []


async def test_entrypoints_and_resolvers_are_a_fake_but_plausible_traefik() -> None:
    adapter = DryRunProxyAdapter()
    eps = await adapter.list_entrypoints()
    assert eps is not None
    assert [(e.name, e.address) for e in eps] == [
        ("web", ":80"),
        ("websecure", ":443"),
        ("traefik", ":8080"),
    ]
    assert await adapter.list_cert_resolvers() == ["letsencrypt"]


async def test_written_routes_show_up_as_healthy_runtime_routers_until_deleted() -> None:
    adapter = DryRunProxyAdapter()
    ref = await adapter.create_route(
        hostname="app.example.com",
        upstream="http://x:1",
        entry_points=["websecure"],
        cert_resolver="letsencrypt",
    )
    routers = await adapter.list_runtime_routers()
    assert routers is not None and len(routers) == 1
    assert routers[0].status == "enabled" and routers[0].entry_points == ("websecure",)
    await adapter.delete_route(ref)
    assert await adapter.list_runtime_routers() == []
