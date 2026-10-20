"""The one validator for a service's manual ``ip_address``.

Used by the YAML schema, the dashboard API and the CLI so all three accept and
refuse exactly the same spellings, and all store the same canonical form (so a
YAML spelling hashes equal to the DB copy — no phantom "unsaved changes").
"""

from __future__ import annotations

import ipaddress


def validate_ip_address(value: object) -> str:
    """Return the canonical address, or "" for blank. Raises ValueError.

    A plain IPv4 or IPv6 address — no CIDR suffix, no hostname, no port: it is
    put into a proxy upstream as ``http://<ip>:<port>`` and pinged as-is.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        raise ValueError(
            f"{value!r} is not an IP address — give a plain IPv4 or IPv6 address "
            "(e.g. 192.168.1.20), without a /prefix, port or hostname"
        ) from None


def url_host(ip: str) -> str:
    """``ip`` as it goes into a URL: IPv6 in brackets."""
    return f"[{ip}]" if ":" in ip else ip


__all__ = ["url_host", "validate_ip_address"]
