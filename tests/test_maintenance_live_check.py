"""The maintenance-page rule is checked against the LIVE route.

Regression: two hosts were "in sync" although their route in Traefik Manager had
no ``maintenance-<service>`` middleware at all - the sync state trusted its own
"applied" record, and the push swallowed Traefik Manager's refusal. Now:
Traefik Manager's reported middlewares decide, a refusal carries Traefik
Manager's own message, and a write is read back.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from orchestrator.adapters.proxy.traefik_manager import (
    TraefikManagerProxyAdapter,
    TraefikManagerUnavailable,
    _response_detail,
    _routes_to_route_info,
)
from orchestrator.db.models import ProxyHost
from orchestrator.registry.proxy_push import (
    DETACHED,
    EXTERNAL,
    FALLBACK_MISSING,
    IN_SYNC,
    desired_signature,
    evaluate,
    fallback_missing,
)
from tests.test_proxy_entrypoints_adapter import _patch, _settings

SLUG = "cloud"
MW = "maintenance-cloud"


def _adapter() -> TraefikManagerProxyAdapter:
    return TraefikManagerProxyAdapter(_settings())


def _apps(
    *, middlewares: list[str] | None, eps: list[str] | None = None, name: str = "cloud"
) -> Any:
    app: dict[str, Any] = {
        "id": f"{name}.yml::{name}",
        "name": name,
        "protocol": "http",
        "rule": "Host(`cloud.example.com`)",
        "servers": ["http://10.0.0.11:80"],
        "entryPoints": eps or ["websecure"],
        "enabled": True,
        "configFile": f"{name}.yml",
    }
    if middlewares is not None:
        app["middlewares"] = middlewares
    return _routes_to_route_info([app])


def test_routes_report_their_middlewares_or_none() -> None:
    assert _apps(middlewares=["cloud-hsts"])[0].middlewares == ("cloud-hsts",)
    assert _apps(middlewares=[])[0].middlewares == ()
    assert _apps(middlewares=None)[0].middlewares is None  # not reported: no conclusion


def test_missing_rule_is_detected_only_when_it_can_be_known() -> None:
    proxy = _adapter()
    assert fallback_missing(proxy, _apps(middlewares=["cloud-hsts"]), SLUG) is True
    assert fallback_missing(proxy, _apps(middlewares=[]), SLUG) is True
    assert fallback_missing(proxy, _apps(middlewares=["cloud-hsts", MW]), SLUG) is False
    assert (
        fallback_missing(proxy, _apps(middlewares=[f"{MW}@file"]), SLUG) is False
    )  # provider suffix
    assert fallback_missing(proxy, _apps(middlewares=None), SLUG) is False  # unknown
    # A plain-HTTP (redirect) router never needs the page.
    assert fallback_missing(proxy, _apps(middlewares=[], eps=["web"]), SLUG) is False
    assert fallback_missing(proxy, _apps(middlewares=[]), None) is False


def _host(ref: str | None = None, **kw: Any) -> ProxyHost:
    extra = {"traefik_manager_route_id": ref} if ref else {}
    return ProxyHost(
        hostname="cloud.example.com",
        upstream="http://10.0.0.11:80",
        service_id=1,
        entry_points=["websecure"],
        extra=extra,
        **kw,
    )


def test_a_hand_written_route_without_the_rule_is_flagged() -> None:
    ph = _host()  # adopted: no route reference of ours
    # The recorded fingerprint says the fallback was applied - the live route says no.
    from orchestrator.registry.proxy_push import fallback_signature

    ph.pushed_sig = fallback_signature(ph, SLUG)
    state = evaluate(_adapter(), ph, _apps(middlewares=["cloud-hsts"]), SLUG)
    assert state.state == EXTERNAL
    assert FALLBACK_MISSING in state.changes and "fallback" in state.actions

    ok = evaluate(_adapter(), ph, _apps(middlewares=["cloud-hsts", MW]), SLUG)
    assert FALLBACK_MISSING not in ok.changes and "fallback" not in ok.actions


def test_an_owned_route_that_looks_in_sync_but_lacks_the_rule_is_detached() -> None:
    ref = "orchestrator-cloud-example-com"  # a route this adapter created
    ph = _host(ref)
    ph.pushed_sig = desired_signature(ph, SLUG)

    missing = evaluate(_adapter(), ph, _apps(middlewares=["cloud-hsts"], name=ref), SLUG)
    assert missing.state == DETACHED
    assert missing.changes == [FALLBACK_MISSING] and missing.actions == ["fallback"]

    present = evaluate(_adapter(), ph, _apps(middlewares=["cloud-hsts", MW], name=ref), SLUG)
    assert present.state == IN_SYNC and not present.actions


def test_a_disabled_host_is_never_flagged() -> None:
    ph = _host()
    ph.enabled = False
    state = evaluate(_adapter(), ph, _apps(middlewares=[]), SLUG)
    assert FALLBACK_MISSING not in state.changes


# --- Traefik Manager's own explanation of a refusal -------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"ok": False, "error": "Middleware x does not exist"}, "Middleware x does not exist"),
        ({"ok": False, "needsConfirm": True}, "needs confirmation"),
        (
            {"ok": False, "missingReferences": [{"kind": "middlewares", "name": "m"}]},
            "missing: middlewares m",
        ),
    ],
)
def test_response_detail_keeps_traefik_managers_message(
    body: dict[str, Any], expected: str
) -> None:
    assert expected in _response_detail(httpx.Response(409, json=body))


def test_response_detail_handles_non_json() -> None:
    assert _response_detail(httpx.Response(500, text="boom")) == "boom"


RAW = "http:\n  routers:\n    cloud:\n      rule: Host(`cloud.example.com`)\n      service: cloud\n"


class RawTM:
    """Just enough Traefik Manager for the raw read-modify-write pair."""

    def __init__(self, *, save_status: int = 200, forget: bool = False) -> None:
        self.raw = RAW
        self.save_status = save_status
        self.forget = forget  # accept the write but do not keep it
        self.saved: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/raw") and request.method == "GET":
            return httpx.Response(200, json={"raw": self.raw, "configFile": "cloud.yml"})
        if request.url.path.endswith("/raw") and request.method == "POST":
            if self.save_status != 200:
                return httpx.Response(
                    self.save_status,
                    json={"ok": False, "error": "route references middleware that is not defined"},
                )
            content = json.loads(request.read())["content"]
            self.saved.append(content)
            if not self.forget:
                self.raw = content
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": True})


async def test_attaching_the_rule_writes_and_reads_it_back(monkeypatch: pytest.MonkeyPatch) -> None:
    tm = RawTM()
    _patch(monkeypatch, tm)
    wrote = await _adapter()._add_middleware_to_route("cloud.yml::cloud", MW)
    assert wrote is True and MW in tm.raw


async def test_a_refused_write_carries_traefik_managers_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, RawTM(save_status=409))
    with pytest.raises(TraefikManagerUnavailable, match="not defined"):
        await _adapter()._add_middleware_to_route("cloud.yml::cloud", MW)


async def test_a_write_that_did_not_stick_is_an_error_not_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, RawTM(forget=True))
    with pytest.raises(TraefikManagerUnavailable, match="not on route"):
        await _adapter()._add_middleware_to_route("cloud.yml::cloud", MW)


async def test_ensure_fallback_raises_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/routes":
            return httpx.Response(500, json={"error": "disk full"})
        return httpx.Response(200, json={"ok": True})

    _patch(monkeypatch, handle)
    adapter = _adapter()
    assert (
        await adapter.ensure_maintenance_fallback(hostname="a.example.com", service_slug="s") == []
    )
    with pytest.raises(TraefikManagerUnavailable, match="disk full"):
        await adapter.ensure_maintenance_fallback(
            hostname="a.example.com", service_slug="s", raise_errors=True
        )


def _routes_response(service: str, known: list[str]) -> dict[str, Any]:
    mw = TraefikManagerProxyAdapter(_settings()).maintenance_middleware_name(SLUG)
    return {
        "services": {"http": known},
        "middlewares": [
            {
                "name": mw,
                "type": "http",
                "yaml": f"errors:\n  status:\n    - 500-599\n  service: {service}\n  query: /maintenance/{SLUG}\n",
            },
            {"name": "cloud-hsts", "type": "http", "yaml": "headers:\n  stsSeconds: 31536000\n"},
        ],
    }


def test_middleware_pointing_at_a_missing_service_is_dangling() -> None:
    from orchestrator.adapters.proxy.traefik_manager import (
        SHARED_BACKEND_SERVICE_NAME,
        SHARED_BACKEND_SERVICE_REF,
        _dangling_middlewares,
    )

    mw = TraefikManagerProxyAdapter(_settings()).maintenance_middleware_name(SLUG)
    # The bug: the middleware named the router, Traefik Manager's service has -service.
    bad = _routes_response(SHARED_BACKEND_SERVICE_NAME, [SHARED_BACKEND_SERVICE_REF])
    assert _dangling_middlewares(bad) == {mw}
    good = _routes_response(SHARED_BACKEND_SERVICE_REF, [SHARED_BACKEND_SERVICE_REF])
    assert _dangling_middlewares(good) == set()
    # Provider-qualified references and an unreported service list prove nothing.
    assert _dangling_middlewares(_routes_response("x@file", [])) == set()
    assert _dangling_middlewares({"middlewares": bad["middlewares"]}) == set()


def test_attached_but_dangling_rule_counts_as_missing() -> None:
    mw = TraefikManagerProxyAdapter(_settings()).maintenance_middleware_name(SLUG)
    (route,) = _routes_to_route_info(
        [
            {
                "id": "cloud.yml::cloud",
                "name": "cloud",
                "protocol": "http",
                "rule": "Host(`cloud.example.com`)",
                "servers": ["http://10.0.0.11:80"],
                "entryPoints": ["websecure"],
                "enabled": True,
                "middlewares": [mw],
            }
        ],
        {mw},
    )
    assert fallback_missing(_adapter(), [route], SLUG) is True


def test_the_middleware_points_at_the_service_traefik_manager_creates() -> None:
    from orchestrator.adapters.proxy.traefik_manager import SHARED_BACKEND_SERVICE_REF

    body = TraefikManagerProxyAdapter._middleware_yaml(SHARED_BACKEND_SERVICE_REF, SLUG)
    assert f"service: {SHARED_BACKEND_SERVICE_REF}" in body
    assert SHARED_BACKEND_SERVICE_REF.endswith("-service")


# -- only Traefik's "could not reach it" answers show the page ------------------------------------


def test_the_rule_catches_only_502_to_504() -> None:
    from orchestrator.adapters.proxy.traefik_manager import SHARED_BACKEND_SERVICE_REF

    body = TraefikManagerProxyAdapter._middleware_yaml(SHARED_BACKEND_SERVICE_REF, SLUG)
    assert "502-504" in body and "500-599" not in body


async def test_an_older_rule_is_rewritten_and_others_are_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator.adapters.proxy.traefik_manager import SHARED_BACKEND_SERVICE_REF

    ref = SHARED_BACKEND_SERVICE_REF
    current = TraefikManagerProxyAdapter._middleware_yaml(ref, "media")
    listed = [
        *_routes_response(ref, [ref])["middlewares"],  # maintenance-cloud, 500-599; cloud-hsts
        {"name": "maintenance-media", "type": "http", "yaml": current},
        {
            "name": "maintenance-other",
            "type": "http",
            "yaml": "errors:\n  status: ['500-599']\n  service: someone-else\n  query: /x\n",
        },
    ]
    saved: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/routes":
            return httpx.Response(200, json={"apps": [], "middlewares": listed})
        if request.url.path == "/save-middleware":
            from urllib.parse import parse_qsl

            saved.append(dict(parse_qsl(request.content.decode())))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    _patch(monkeypatch, handle)

    events = await _adapter().refresh_maintenance_rules()

    assert [(e.name, e.details["was"]) for e in events] == [(MW, ["500-599"])]
    assert [s["middlewareName"] for s in saved] == [MW]
    assert "502-504" in saved[0]["middlewareContent"]
    assert f"/maintenance/{SLUG}" in saved[0]["middlewareContent"]
