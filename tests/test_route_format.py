"""Formatting proxy routes: hand-written / migrated routes rewritten into the standard shape,
one host at a time, with a snapshot first and everything put back when a step fails."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlmodel import Session

from orchestrator.adapters.proxy.traefik_manager import TraefikManagerProxyAdapter
from orchestrator.config import Settings
from orchestrator.db.models import ProxyHost, Service
from orchestrator.domain.enums import RouterProvider
from orchestrator.registry import route_format as rf
from orchestrator.registry.proxy_push import TRAEFIK_MANAGER_ROUTE_REF_KEY
from orchestrator.registry.route_standard import analyze, standard_options
from tests.sim_tm import SimTM
from tests.test_proxy_entrypoints_adapter import _patch, _settings

HOST = "nas.example.com"
UPSTREAM = "http://10.0.0.15:80"
LEGACY = ("redirect-to-https", "hsts-headers")


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session: Session,
    service: Service,
    **sim_kw: Any,
) -> tuple[SimTM, SimpleNamespace, Settings]:
    sim = SimTM(files=("dynamic.yml", "legacy.yml"), config_dir=True, **sim_kw)
    _patch(monkeypatch, sim)
    settings = _settings(traefik_manager_config_file="{service}.yml", state_dir=tmp_path)
    return sim, SimpleNamespace(proxy=TraefikManagerProxyAdapter(settings)), settings


def _host(session: Session, service: Service, hostname: str = HOST, **kw: Any) -> ProxyHost:
    ph = ProxyHost(
        hostname=hostname,
        upstream=kw.pop("upstream", UPSTREAM),
        router_provider=kw.pop("router_provider", RouterProvider.TRAEFIK),
        service_id=service.id,
        entry_points=kw.pop("entry_points", ["websecure"]),
        cert_resolver=kw.pop("cert_resolver", "letsencrypt"),
        **kw,
    )
    session.add(ph)
    session.commit()
    session.refresh(ph)
    return ph


def _old(sim: SimTM, name: str = "nas", hostname: str = HOST, **kw: Any) -> None:
    kw.setdefault("middlewares", (*LEGACY, "auth-basic"))
    kw.setdefault("resolver", "letsencrypt")
    sim.add("legacy.yml", name, hostname, upstream=UPSTREAM, **kw)


async def _plan(session: Session, adapters: Any, settings: Settings, **kw: Any) -> rf.FormatPlan:
    return await rf.build_plan(session, adapters, settings, **kw)


# -- the standard and the analysis ---------------------------------------------------------------


def test_standard_options_follow_the_new_host_form() -> None:
    opts = standard_options({"skip_upstream_tls_verify": True}, redirect_available=True)
    assert opts.pass_host_header and opts.http_redirect and opts.skip_upstream_tls_verify
    assert not standard_options(None, redirect_available=False).http_redirect
    assert not standard_options(None, redirect_available=None).http_redirect  # unknown: not assumed


def test_analysis_accepts_a_plain_route_and_refuses_anything_it_would_lose() -> None:
    base = (
        "http:\n  routers:\n    r:\n      rule: Host(`a.example.com`)\n      entryPoints: [websecure]\n"
        "      service: r\n  services:\n    r:\n      loadBalancer:\n        servers:\n"
        "          - url: http://10.0.0.1:80\n"
    )
    assert analyze(base, "a.example.com").simple
    for broken, why in (
        (
            base.replace("Host(`a.example.com`)", "Host(`a.example.com`) && PathPrefix(`/x`)"),
            "not just a host",
        ),
        (base.replace("      service: r\n", "      service: r\n      priority: 10\n"), "priority"),
        (
            base.replace(
                "- url: http://10.0.0.1:80\n",
                "- url: http://10.0.0.1:80\n          - url: http://10.0.0.2:80\n",
            ),
            "2 servers",
        ),
        (base.replace("service: r", "service: shared@file", 1), "shared service"),
        (base.replace("        servers:", "        sticky: {}\n        servers:"), "sticky"),
    ):
        got = analyze(broken, "a.example.com")
        assert not got.simple and why in " ".join(got.problems), (why, got.problems)


# -- the plan ------------------------------------------------------------------------------------


async def test_plan_classifies_every_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session: Session, service: Service
) -> None:
    sim, adapters, settings = _setup(monkeypatch, tmp_path, session, service)
    _old(sim)
    _host(session, service)
    _old(sim, "complex", "path.example.com", rule="Host(`path.example.com`) && PathPrefix(`/a`)")
    _host(session, service, "path.example.com")
    _host(session, service, "off.example.com", enabled=False)
    _host(session, service, "nowhere.example.com")
    _host(session, service, "other.example.com", router_provider=RouterProvider.OTHER, upstream="")
    plan = await _plan(session, adapters, settings)
    by = {i.hostname: i for i in plan.items}
    nas = by[HOST]
    assert nas.status == rf.READY and nas.action == rf.RECREATE
    assert (
        nas.target["name"] == "orchestrator-nas-example-com" and nas.target["file"] == "thing.yml"
    )
    assert nas.dropped_middlewares == ["redirect-to-https"]
    assert nas.target["middlewares"] == ["hsts-headers", "auth-basic"]  # only the redirect one goes
    assert nas.target["options"]["http_redirect"] is True
    assert any("redirect router" in c for c in nas.changes)
    assert by["path.example.com"].status == rf.BLOCKED
    assert "not just a host" in by["path.example.com"].reasons[0]
    assert (
        by["off.example.com"].status == rf.SKIPPED
        and "stays off" in by["off.example.com"].reasons[0]
    )
    assert (
        by["nowhere.example.com"].status == rf.SKIPPED
        and "push it first" in by["nowhere.example.com"].reasons[0]
    )
    assert by["other.example.com"].status == rf.SKIPPED


async def test_plan_reports_a_route_that_is_already_standard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session: Session, service: Service
) -> None:
    sim, adapters, settings = _setup(monkeypatch, tmp_path, session, service)
    _old(sim)
    _host(session, service)
    plan = await _plan(session, adapters, settings)
    run = await rf.apply_plan(session, adapters, settings, plan, actor="t")
    assert [r.outcome for r in run] == ["formatted"]
    again = await _plan(session, adapters, settings)
    assert again.items[0].status == rf.STANDARD


# -- apply ---------------------------------------------------------------------------------------


async def test_apply_recreates_the_route_in_the_standard_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session: Session, service: Service
) -> None:
    sim, adapters, settings = _setup(monkeypatch, tmp_path, session, service)
    _old(sim)
    ph = _host(session, service)
    plan = await _plan(session, adapters, settings)
    (result,) = await rf.apply_plan(session, adapters, settings, plan, actor="storm")
    assert result.ok, result.message
    new = "orchestrator-nas-example-com"
    assert sim.file_of(new) == "thing.yml"
    assert sim.file_of("nas") is None  # the old route is gone from its file
    assert sim.file_of(f"{new}-redirect") == "thing.yml"  # the redirect router exists
    assert sim.files["thing.yml"][new]["mws"] == ["hsts-headers", "auth-basic"]
    session.refresh(ph)
    assert ph.extra[TRAEFIK_MANAGER_ROUTE_REF_KEY] == new
    assert ph.route_options["http_redirect"] is True and ph.entry_points == ["websecure"]
    snap = json.loads(next((tmp_path / "route-snapshots").rglob(f"{HOST}.json")).read_text())
    assert "PathPrefix" not in snap["routes"][0]["raw"] and "nas" in snap["routes"][0]["raw"]


async def test_a_route_the_runtime_refuses_is_removed_and_the_old_one_stays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session: Session, service: Service
) -> None:
    sim, adapters, settings = _setup(monkeypatch, tmp_path, session, service)
    sim.runtime_errors.add("orchestrator-nas-example-com")
    _old(sim)
    ph = _host(session, service)
    plan = await _plan(session, adapters, settings)
    (result,) = await rf.apply_plan(session, adapters, settings, plan, actor="storm")
    assert not result.ok and result.outcome == "restored"
    assert sim.file_of("orchestrator-nas-example-com") is None  # the new one was taken away
    assert sim.file_of("nas") == "legacy.yml"  # the old one never stopped existing
    session.refresh(ph)
    assert TRAEFIK_MANAGER_ROUTE_REF_KEY not in (ph.extra or {})


async def test_a_failed_delete_of_the_old_route_puts_things_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session: Session, service: Service
) -> None:
    sim, adapters, settings = _setup(monkeypatch, tmp_path, session, service)
    sim.fail_delete.add("nas")
    _old(sim)
    _host(session, service)
    plan = await _plan(session, adapters, settings)
    (result,) = await rf.apply_plan(session, adapters, settings, plan, actor="storm")
    assert not result.ok and result.outcome == "restored"
    assert sim.file_of("nas") == "legacy.yml"
    assert sim.file_of("orchestrator-nas-example-com") is None


async def test_a_route_that_changed_since_the_plan_is_not_touched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session: Session, service: Service
) -> None:
    sim, adapters, settings = _setup(monkeypatch, tmp_path, session, service)
    _old(sim)
    _host(session, service)
    plan = await _plan(session, adapters, settings)
    sim.files["legacy.yml"]["nas"]["mws"].append("someone-added-this")
    (result,) = await rf.apply_plan(session, adapters, settings, plan, actor="storm")
    assert not result.ok and "changed since the plan" in result.message
    assert (
        sim.file_of("nas") == "legacy.yml" and sim.file_of("orchestrator-nas-example-com") is None
    )


async def test_the_run_stops_at_the_first_failure_unless_told_to_go_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session: Session, service: Service
) -> None:
    sim, adapters, settings = _setup(monkeypatch, tmp_path, session, service)
    sim.runtime_errors.add("orchestrator-a-example-com")
    for n in ("a", "b"):
        _old(sim, n, f"{n}.example.com")
        _host(session, service, f"{n}.example.com")
    plan = await _plan(session, adapters, settings)
    results = await rf.apply_plan(session, adapters, settings, plan, actor="t")
    assert [r.hostname for r in results] == ["a.example.com"] and not results[0].ok
    results = await rf.apply_plan(
        session, adapters, settings, plan, actor="t", stop_on_failure=False
    )
    assert [r.ok for r in results] == [False, True]  # b went through


async def test_hosts_that_are_off_are_left_exactly_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session: Session, service: Service
) -> None:
    sim, adapters, settings = _setup(monkeypatch, tmp_path, session, service)
    _old(sim)
    _host(session, service, enabled=False)
    plan = await _plan(session, adapters, settings)
    assert await rf.apply_plan(session, adapters, settings, plan, actor="t") == []
    assert sim.file_of("nas") == "legacy.yml" and not sim.calls("save")


async def test_an_owned_route_in_the_wrong_file_is_moved_and_gets_its_redirect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session: Session, service: Service
) -> None:
    sim, adapters, settings = _setup(monkeypatch, tmp_path, session, service)
    sim.add(
        "dynamic.yml",
        "orchestrator-nas-example-com",
        HOST,
        upstream=UPSTREAM,
        resolver="letsencrypt",
    )
    _host(session, service, extra={TRAEFIK_MANAGER_ROUTE_REF_KEY: "orchestrator-nas-example-com"})
    plan = await _plan(session, adapters, settings)
    (item,) = plan.items
    assert item.status == rf.READY and item.action == rf.MOVE
    (result,) = await rf.apply_plan(session, adapters, settings, plan, actor="t")
    assert result.ok, result.message
    assert sim.file_of("orchestrator-nas-example-com") == "thing.yml"
