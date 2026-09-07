"""Liveness and readiness endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator.web.app import create_app


def test_healthz_returns_ok() -> None:
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "version" in body


def test_readyz_returns_ready_when_db_reachable() -> None:
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"
