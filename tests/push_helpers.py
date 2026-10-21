"""Small helpers for tests that exercise the deferred push (``POST /api/push``)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


def push(client: TestClient, **body: Any) -> dict[str, Any]:
    """Run a push and return the response body (asserts the HTTP call worked;
    per-item failures are in ``results``)."""
    r = client.post("/api/push", json=body)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def actions(result: dict[str, Any], *, ok: bool | None = True) -> list[tuple[str, str]]:
    """``[(hostname, action)]`` of a push response, optionally only the ok ones."""
    return [
        (item["hostname"], item["action"])
        for item in result["results"]
        if ok is None or item["ok"] is ok
    ]


def failures(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in result["results"] if not item["ok"]]


def sync_state(client: TestClient, host_id: int) -> str:
    return str(client.get(f"/api/proxy-hosts/{host_id}").json()["sync_state"])
