"""Traefik drift polling: rule parsing, HTTP failure handling, classification."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from orchestrator.config import Settings
from orchestrator.registry.traefik_status import (
    DriftStatus,
    drift_status,
    extract_hostnames,
    fetch_router_hostnames,
)

# ---------------------------------------------------------------------------
# extract_hostnames
# ---------------------------------------------------------------------------


def test_extracts_a_single_host() -> None:
    assert extract_hostnames("Host(`app.example.lan`)") == {"app.example.lan"}


def test_extracts_multiple_hosts_ored_together() -> None:
    rule = "Host(`a.example.lan`) || Host(`b.example.lan`)"
    assert extract_hostnames(rule) == {"a.example.lan", "b.example.lan"}


def test_extracts_hosts_combined_with_other_predicates() -> None:
    rule = "Host(`app.example.lan`) && PathPrefix(`/api`)"
    assert extract_hostnames(rule) == {"app.example.lan"}


def test_extracts_multiple_hosts_in_one_clause() -> None:
    rule = "Host(`a.example.lan`, `b.example.lan`)"
    assert extract_hostnames(rule) == {"a.example.lan", "b.example.lan"}


def test_lowercases_and_strips_trailing_dot() -> None:
    assert extract_hostnames("Host(`App.Example.LAN.`)") == {"app.example.lan"}


def test_rule_without_a_host_clause_yields_nothing() -> None:
    assert extract_hostnames("PathPrefix(`/healthz`)") == set()


def test_hostsni_clause_is_also_recognised() -> None:
    assert extract_hostnames("HostSNI(`tcp.example.lan`)") == {"tcp.example.lan"}


# ---------------------------------------------------------------------------
# drift_status
# ---------------------------------------------------------------------------


def test_unknown_when_no_poll_result() -> None:
    assert drift_status("app.example.lan", None) == DriftStatus.UNKNOWN


def test_matched_when_hostname_present() -> None:
    assert drift_status("app.example.lan", {"app.example.lan"}) == DriftStatus.MATCHED


def test_matched_is_case_insensitive() -> None:
    assert drift_status("App.Example.LAN", {"app.example.lan"}) == DriftStatus.MATCHED


def test_missing_when_hostname_absent_but_poll_succeeded() -> None:
    assert drift_status("app.example.lan", {"other.example.lan"}) == DriftStatus.MISSING


def test_missing_when_poll_succeeded_but_empty() -> None:
    assert drift_status("app.example.lan", set()) == DriftStatus.MISSING


# ---------------------------------------------------------------------------
# fetch_router_hostnames
# ---------------------------------------------------------------------------


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Make every httpx.AsyncClient created by the module use a mock transport."""
    import orchestrator.registry.traefik_status as ts_mod

    real_async_client = httpx.AsyncClient

    class _PatchedAsyncClient(real_async_client):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ts_mod.httpx, "AsyncClient", _PatchedAsyncClient)


async def test_disabled_when_url_not_configured() -> None:
    settings = Settings(traefik_api_url="")
    assert await fetch_router_hostnames(settings) is None


async def test_parses_routers_into_a_hostname_set(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/http/routers"
        return httpx.Response(
            200,
            json=[
                {
                    "name": "app@docker",
                    "rule": "Host(`app.example.lan`)",
                    "status": "enabled",
                    "service": "app-svc",
                },
                {
                    "name": "other@docker",
                    "rule": "Host(`a.example.lan`) || Host(`b.example.lan`)",
                    "status": "enabled",
                    "service": "other-svc",
                },
            ],
        )

    _patch_transport(monkeypatch, handle)
    settings = Settings(traefik_api_url="http://traefik.test:8080")
    hostnames = await fetch_router_hostnames(settings)
    assert hostnames == {"app.example.lan", "a.example.lan", "b.example.lan"}


async def test_connection_failure_returns_none_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch_transport(monkeypatch, handle)
    settings = Settings(traefik_api_url="http://traefik.test:8080")
    assert await fetch_router_hostnames(settings) is None


async def test_timeout_returns_none_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    _patch_transport(monkeypatch, handle)
    settings = Settings(traefik_api_url="http://traefik.test:8080", traefik_api_timeout_s=0.01)
    assert await fetch_router_hostnames(settings) is None


async def test_non_2xx_returns_none_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    _patch_transport(monkeypatch, handle)
    settings = Settings(traefik_api_url="http://traefik.test:8080")
    assert await fetch_router_hostnames(settings) is None


async def test_bad_json_returns_none_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    _patch_transport(monkeypatch, handle)
    settings = Settings(traefik_api_url="http://traefik.test:8080")
    assert await fetch_router_hostnames(settings) is None


async def test_non_list_json_returns_none_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    _patch_transport(monkeypatch, handle)
    settings = Settings(traefik_api_url="http://traefik.test:8080")
    assert await fetch_router_hostnames(settings) is None


async def test_router_without_a_rule_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"name": "weird@internal", "service": "noop@internal"}])

    _patch_transport(monkeypatch, handle)
    settings = Settings(traefik_api_url="http://traefik.test:8080")
    assert await fetch_router_hostnames(settings) == set()
