"""Shared client behaviour: auth header shape and error mapping.

The auth header separator differs between PVE (``=``) and PBS (``:``). Getting
it wrong produces an opaque 401 against real hardware, so it is pinned here.
"""

from __future__ import annotations

import httpx
import pytest

from orchestrator.adapters.errors import (
    AdapterAuthError,
    AdapterRequestError,
    AdapterUnreachable,
)
from orchestrator.adapters.proxmox_family import ProxmoxFamilyClient, build_auth_header


def test_pve_auth_header_uses_equals_separator() -> None:
    header = build_auth_header("pve", "orchestrator@pve!backups", "s3cr3t")
    assert header == "PVEAPIToken=orchestrator@pve!backups=s3cr3t"


def test_pbs_auth_header_uses_colon_separator() -> None:
    header = build_auth_header("pbs", "orchestrator@pbs!datastore", "s3cr3t")
    assert header == "PBSAPIToken=orchestrator@pbs!datastore:s3cr3t"


def _client_with(handler: httpx.MockTransport) -> ProxmoxFamilyClient:
    client = ProxmoxFamilyClient(
        flavor="pve",
        host="pve.test",
        port=8006,
        token_id="orchestrator@pve!t",
        token_secret="secret",
        verify_tls=False,
    )
    client._client = httpx.AsyncClient(
        base_url="https://pve.test:8006/api2/json",
        headers={"Authorization": build_auth_header("pve", "orchestrator@pve!t", "secret")},
        transport=handler,
    )
    return client


async def test_unwraps_data_envelope() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"version": "8.2.2"}})

    client = _client_with(httpx.MockTransport(handle))
    assert await client.request("GET", "/version") == {"version": "8.2.2"}


async def test_sends_the_auth_header() -> None:
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"data": None})

    client = _client_with(httpx.MockTransport(handle))
    await client.request("GET", "/version")
    assert seen["auth"] == "PVEAPIToken=orchestrator@pve!t=secret"


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_raise_adapter_auth_error(status: int) -> None:
    client = _client_with(httpx.MockTransport(lambda r: httpx.Response(status)))
    with pytest.raises(AdapterAuthError):
        await client.request("GET", "/version")


async def test_other_http_errors_raise_request_error() -> None:
    client = _client_with(httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(AdapterRequestError) as exc:
        await client.request("GET", "/version")
    assert exc.value.status_code == 500


async def test_transport_failure_maps_to_unreachable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = _client_with(httpx.MockTransport(handle))
    with pytest.raises(AdapterUnreachable):
        await client.request("GET", "/version")


async def test_timeout_maps_to_unreachable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    client = _client_with(httpx.MockTransport(handle))
    with pytest.raises(AdapterUnreachable):
        await client.request("GET", "/version")


async def test_none_params_are_dropped() -> None:
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["query"] = str(request.url.query.decode())
        return httpx.Response(200, json={"data": []})

    client = _client_with(httpx.MockTransport(handle))
    await client.request("GET", "/snapshots", params={"backup-id": "9001", "backup-type": None})
    assert "backup-id=9001" in seen["query"]
    assert "backup-type" not in seen["query"]
