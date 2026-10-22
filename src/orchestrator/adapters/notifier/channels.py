"""The real channels. Each is a :class:`Notifier` for exactly one destination."""

from __future__ import annotations

import asyncio
import base64
import smtplib
from email.message import EmailMessage

import httpx

from orchestrator.adapters.notifier.base import Notifier, NotifySeverity
from orchestrator.logging_setup import get_logger

log = get_logger(__name__)

_NTFY_PRIORITY = {
    NotifySeverity.INFO: "3",
    NotifySeverity.WARN: "4",
    NotifySeverity.ERROR: "4",
    NotifySeverity.CRITICAL: "5",
}
_NTFY_TAG = {
    NotifySeverity.INFO: "information_source",
    NotifySeverity.WARN: "warning",
    NotifySeverity.ERROR: "x",
    NotifySeverity.CRITICAL: "rotating_light",
}


def _header_text(value: str) -> str:
    """HTTP headers are latin-1; ntfy reads RFC 2047 encoded words for the rest."""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value.encode()).decode() + "?="


class NtfyNotifier(Notifier):
    """POST to an ntfy topic URL (https://ntfy.sh/mytopic or your own server)."""

    def __init__(self, url: str, token: str = "", *, timeout_s: float = 10.0) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout_s

    def channels(self) -> list[str]:
        return ["ntfy"]

    async def notify(
        self,
        *,
        severity: NotifySeverity,
        title: str,
        body: str,
        tags: list[str] | None = None,
        approve_url: str | None = None,
    ) -> None:
        headers = {
            "Title": _header_text(title[:250]),
            "Priority": _NTFY_PRIORITY[severity],
            "Tags": ",".join([_NTFY_TAG[severity], *(tags or [])]),
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if approve_url:
            headers["Actions"] = f"view, Open, {approve_url}"
            headers["Click"] = approve_url
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(self._url, content=body.encode(), headers=headers)
        response.raise_for_status()


class WebhookNotifier(Notifier):
    """POST ``{severity, title, body, tags, approve_url}`` as JSON."""

    def __init__(self, url: str, token: str = "", *, timeout_s: float = 10.0) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout_s

    def channels(self) -> list[str]:
        return ["webhook"]

    async def notify(
        self,
        *,
        severity: NotifySeverity,
        title: str,
        body: str,
        tags: list[str] | None = None,
        approve_url: str | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        payload = {
            "severity": severity.value,
            "title": title,
            "body": body,
            "tags": tags or [],
            "approve_url": approve_url,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(self._url, json=payload, headers=headers)
        response.raise_for_status()


class SmtpNotifier(Notifier):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        recipients: list[str],
        starttls: bool = True,
        timeout_s: float = 15.0,
    ) -> None:
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._sender, self._recipients = sender, recipients
        self._starttls = starttls
        self._timeout = timeout_s

    def channels(self) -> list[str]:
        return ["email"]

    def _send(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
            if self._starttls:
                smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(message)

    async def notify(
        self,
        *,
        severity: NotifySeverity,
        title: str,
        body: str,
        tags: list[str] | None = None,
        approve_url: str | None = None,
    ) -> None:
        message = EmailMessage()
        message["Subject"] = f"[orchestrator {severity.value}] {title}"
        message["From"] = self._sender
        message["To"] = ", ".join(self._recipients)
        text = body + (f"\n\nOpen: {approve_url}" if approve_url else "")
        message.set_content(text)
        await asyncio.to_thread(self._send, message)
