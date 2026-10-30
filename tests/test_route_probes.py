"""Route checks (``traefik:<host>``) accept 2xx, 3xx, 401 and 403 - not a 404, which is what
Traefik answers when no route matches - and HTTP probes take whole status classes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlmodel import Session, select

from orchestrator.db.models import Probe, Service
from orchestrator.domain.enums import ProbeKind
from orchestrator.domain.schemas import HttpProbeConfig
from orchestrator.registry import traefik_probes


def test_status_classes_and_codes() -> None:
    cfg = HttpProbeConfig(url="https://10.0.0.2/", expect_status=["2xx", "3XX", 401, "403"])
    assert cfg.expect_status == ["2xx", "3xx", 401, 403]
    assert cfg.accepts(200) and cfg.accepts(204) and cfg.accepts(302)
    assert cfg.accepts(401) and cfg.accepts(403)
    assert not cfg.accepts(404) and not cfg.accepts(502)
    with pytest.raises(ValidationError):
        HttpProbeConfig(url="https://10.0.0.2/", expect_status=["2x"])
    with pytest.raises(ValidationError):
        HttpProbeConfig(url="https://10.0.0.2/", expect_status=[])


def test_old_route_probes_are_tightened_once(session: Session, service: Service) -> None:
    old = list(range(200, 500))
    session.add_all(
        [
            Probe(service_id=service.id, name="traefik:a.example.lan", kind=ProbeKind.HTTP,
                  config={"url": "https://10.0.0.2/", "host_header": "a.example.lan",
                          "expect_status": old, "method": "GET"}),
            Probe(service_id=service.id, name="traefik:b.example.lan", kind=ProbeKind.HTTP,
                  config={"url": "https://10.0.0.2/", "expect_status": [200, 404]}),
            Probe(service_id=service.id, name="http", kind=ProbeKind.HTTP,
                  config={"url": "https://10.0.0.2/", "expect_status": old}),
        ]
    )  # fmt: skip
    session.commit()

    assert traefik_probes.tighten_old(session) == 1
    by = {p.name: p.config for p in session.exec(select(Probe)).all()}
    assert by["traefik:a.example.lan"]["expect_status"] == ["2xx", "3xx", 401, 403]
    assert by["traefik:a.example.lan"]["method"] == "GET"  # the rest is kept
    assert by["traefik:b.example.lan"]["expect_status"] == [200, 404]  # edited by hand: kept
    assert by["http"]["expect_status"] == old  # not a route probe
    assert traefik_probes.tighten_old(session) == 0
