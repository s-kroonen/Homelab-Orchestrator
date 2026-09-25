"""Shared HTTP client for the Proxmox family of APIs (PVE and PBS).

Both speak the same envelope — ``{"data": ...}`` under ``/api2/json`` — and both
authenticate with an API token header.  The **separator differs** and it is the
single most common cause of a mystifying 401:

    PVE:  Authorization: PVEAPIToken=USER@REALM!TOKENID=SECRET     <- '='
    PBS:  Authorization: PBSAPIToken=USER@REALM!TOKENID:SECRET     <- ':'

Both hosts commonly present self-signed certificates.  ``verify_tls=False`` is
supported for that case and logs a warning once at construction, because a
silently unverified control-plane connection is worth being loud about.
"""

from __future__ import annotations

import ssl
from typing import Any, Literal

import httpx

from orchestrator.adapters.errors import (
    AdapterAuthError,
    AdapterRequestError,
    AdapterTlsError,
    AdapterUnreachable,
)
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

Flavor = Literal["pve", "pbs"]

# (header scheme, secret separator) per flavour.
_AUTH_FORMAT: dict[Flavor, tuple[str, str]] = {
    "pve": ("PVEAPIToken", "="),
    "pbs": ("PBSAPIToken", ":"),
}


def build_auth_header(flavor: Flavor, token_id: str, token_secret: str) -> str:
    scheme, sep = _AUTH_FORMAT[flavor]
    return f"{scheme}={token_id}{sep}{token_secret}"


class ProxmoxFamilyClient:
    """Thin async wrapper. Unwraps the ``data`` envelope and maps transport
    failures onto the adapter exception hierarchy."""

    def __init__(
        self,
        *,
        flavor: Flavor,
        host: str,
        port: int,
        token_id: str,
        token_secret: str,
        verify_tls: bool = True,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
    ) -> None:
        self._flavor = flavor
        self._base_url = f"https://{host}:{port}/api2/json"
        self._verify_tls = verify_tls
        self._headers = {
            "Authorization": build_auth_header(flavor, token_id, token_secret),
            "Accept": "application/json",
        }
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=10.0,
            pool=5.0,
        )
        self._client: httpx.AsyncClient | None = None

        if not verify_tls:
            log.warning(
                "adapter.tls_verification_disabled",
                flavor=flavor,
                host=host,
                hint="Set *_VERIFY_TLS=true once the CA is trusted.",
            )

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                verify=self._verify_tls,
                timeout=self._timeout,
            )

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """Perform a request and return the unwrapped ``data`` payload.

        ``data`` is form-encoded — both APIs expect ``application/x-www-form-
        urlencoded`` for writes, not JSON.
        """
        if self._client is None:
            await self.start()
        assert self._client is not None

        try:
            response = await self._client.request(
                method,
                path,
                params=_clean(params),
                data=_clean(data),
            )
        except httpx.TimeoutException as exc:
            raise AdapterUnreachable(f"{self._flavor} timed out on {method} {path}: {exc}") from exc
        except httpx.TransportError as exc:
            # A TLS trust failure is NOT a routing problem — the socket
            # connected. Say so, and name the setting that fixes it.
            if _is_tls_trust_failure(exc):
                env_var = "PROXMOX_VERIFY_TLS" if self._flavor == "pve" else "PBS_VERIFY_TLS"
                raise AdapterTlsError(
                    f"{self._flavor} TLS certificate was rejected on {method} {path}: "
                    f"{exc}. The host is reachable — this is a certificate trust "
                    f"problem. Proxmox and PBS ship self-signed certificates by "
                    f"default. Either set {env_var}=false, or install the host's CA "
                    f"into the container's trust store."
                ) from exc
            raise AdapterUnreachable(
                f"{self._flavor} unreachable on {method} {path}: {exc}"
            ) from exc

        if response.status_code in (401, 403):
            detail = response.text.strip()[:500]
            raise AdapterAuthError(
                f"{self._flavor} rejected the API token on {method} {path} "
                f"({response.status_code})"
                + (f": {detail}" if detail else "")
                + ". NOTE: with privilege separation a token's effective privileges "
                "are the INTERSECTION of the owning user's ACL and the token's own "
                "ACL — an ACL on only one of the two grants nothing. Check both with "
                "`proxmox-backup-manager acl list` / `pveum acl list`.",
                status_code=response.status_code,
                body=detail,
            )

        if response.status_code >= 400:
            raise AdapterRequestError(
                f"{self._flavor} returned {response.status_code} on {method} {path}",
                status_code=response.status_code,
                body=response.text[:2000],
            )

        if not response.content:
            return None

        try:
            payload = response.json()
        except ValueError as exc:
            raise AdapterRequestError(
                f"{self._flavor} returned non-JSON on {method} {path}",
                status_code=response.status_code,
                body=response.text[:2000],
            ) from exc

        return payload.get("data") if isinstance(payload, dict) else payload


def _is_tls_trust_failure(exc: Exception) -> bool:
    """Walk the exception chain looking for an SSL certificate verification error."""
    seen = 0
    cur: BaseException | None = exc
    while cur is not None and seen < 10:
        if isinstance(cur, ssl.SSLCertVerificationError | ssl.SSLError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(cur) or "certificate verify failed" in str(cur):
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False


def _clean(d: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop ``None`` values — both APIs treat a present-but-empty param as a
    real value, which is rarely what the caller meant."""
    if d is None:
        return None
    return {k: v for k, v in d.items() if v is not None}
