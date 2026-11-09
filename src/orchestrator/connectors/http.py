"""One HTTP client for connectors: the same timeouts, TLS rules, auth and errors everywhere.

New connectors talk HTTP through :class:`ApiClient` instead of their own ``httpx`` calls, so
that every source:

* times out the same way (``timeout_s``, default 15 s) and says so;
* checks TLS unless its connection says not to (a self-signed homelab certificate);
* sends its credential in one header, which is never logged;
* raises the same errors the adapters raise (``adapters/errors.py``): unreachable, TLS,
  auth (401/403), not found / request error (other 4xx/5xx), with the status and a short body.

Existing adapters keep their own clients for now; they move onto this one as they are touched.
"""

from __future__ import annotations

import ssl
from typing import Any

import httpx

from orchestrator.adapters.errors import (
    AdapterAuthError,
    AdapterRequestError,
    AdapterTlsError,
    AdapterUnreachable,
)
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)


class ApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        verify_tls: bool = True,
        timeout_s: float = 15.0,
        name: str = "api",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.name = name
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Accept": "application/json", **(headers or {})},
            verify=verify_tls,
            timeout=timeout_s,
        )
        self.timeout_s = timeout_s

    async def close(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """The JSON answer (or text, when it is not JSON). Raises an ``AdapterError``."""
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            if isinstance(exc.__cause__, ssl.SSLError) or "CERTIFICATE" in str(exc).upper():
                raise AdapterTlsError(f"{self.name}: TLS certificate not trusted ({exc})") from exc
            raise AdapterUnreachable(
                f"{self.name}: cannot connect to {self.base_url} ({exc})"
            ) from exc
        except httpx.TimeoutException as exc:
            raise AdapterUnreachable(f"{self.name}: no answer in {int(self.timeout_s)} s") from exc
        except httpx.HTTPError as exc:
            raise AdapterUnreachable(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        log.debug(
            "connector.http", api=self.name, method=method, path=path, status=resp.status_code
        )
        body = resp.text[:300]
        if resp.status_code in (401, 403):
            raise AdapterAuthError(
                f"{self.name}: not allowed ({resp.status_code})",
                status_code=resp.status_code,
                body=body,
            )
        if resp.status_code >= 400:
            raise AdapterRequestError(
                f"{self.name}: {method} {path} answered {resp.status_code}",
                status_code=resp.status_code,
                body=body,
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self.request("POST", path, **kwargs)


__all__ = ["ApiClient"]
