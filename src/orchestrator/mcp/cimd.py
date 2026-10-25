"""Client ID Metadata Documents: the client_id is an https URL that serves the client's own
metadata (name, redirect URIs). Only fetched from hosts the operator trusts (default: Claude's),
never from a private address, with a short timeout and a size limit."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

from orchestrator.config import Settings
from orchestrator.mcp.store import OAuthError

MAX_BYTES = 64 * 1024
TIMEOUT_S = 5.0


def allowed_hosts(settings: Settings) -> set[str]:
    return {h.strip().lower() for h in settings.mcp_cimd_hosts.split(",") if h.strip()}


def is_cimd_id(client_id: str) -> bool:
    return client_id.startswith("https://")


def _private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return True
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return True
    return False


async def fetch_client_metadata(settings: Settings, client_id: str) -> dict[str, Any]:
    parts = urlsplit(client_id)
    host = (parts.hostname or "").lower()
    if parts.username or parts.password or not parts.path or parts.path == "/" or parts.fragment:
        raise OAuthError("invalid_client", "client_id is not a metadata document URL")
    if host not in allowed_hosts(settings):
        raise OAuthError("invalid_client", f"client metadata is not fetched from {host}")
    if await asyncio.to_thread(_private, host):
        raise OAuthError("invalid_client", f"{host} does not resolve to a public address")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False) as http:
            resp = await http.get(client_id, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OAuthError("invalid_client", f"could not fetch the client metadata: {exc}") from exc
    if resp.status_code != 200 or len(resp.content) > MAX_BYTES:
        raise OAuthError("invalid_client", "client metadata document is not usable")
    try:
        doc = resp.json()
    except ValueError as exc:
        raise OAuthError("invalid_client", "client metadata is not JSON") from exc
    if not isinstance(doc, dict) or doc.get("client_id") != client_id:
        raise OAuthError("invalid_client", "client metadata client_id does not match its URL")
    uris = doc.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        raise OAuthError("invalid_client", "client metadata has no redirect_uris")
    return doc
