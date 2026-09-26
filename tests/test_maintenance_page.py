"""The maintenance responder: renders HTML, never 5xxs, dedupes concurrent hits.

Runs through the real app + dry-run adapters (like test_healthz.py), not the
pipeline directly — this is about what the web layer does with it.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator import config as config_module
from orchestrator.db import session as session_module
from orchestrator.db.models import PipelineRun, Service
from orchestrator.db.session import get_engine
from orchestrator.web.app import create_app

EXAMPLE = Path(__file__).parent.parent / "config" / "services.example.yaml"


@pytest.fixture
def app_with_registry(monkeypatch: pytest.MonkeyPatch):
    from orchestrator.config import get_settings

    monkeypatch.setenv("WAKE_POLL_INTERVAL_S", "0.001")
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()  # bypass alembic, same as the `session` fixture
    shutil.copy(EXAMPLE, get_settings().services_yaml_path)
    return create_app()


def test_unknown_service_shows_an_info_page(app_with_registry) -> None:
    with TestClient(app_with_registry) as client:
        r = client.get("/maintenance/does-not-exist")

    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Unknown service" in r.text


def test_disabled_service_shows_not_managed(app_with_registry) -> None:
    from sqlmodel import Session, select

    with TestClient(app_with_registry) as client:
        with Session(get_engine()) as session:
            svc = session.exec(select(Service).where(Service.slug == "example-media")).one()
            svc.enabled = False
            session.add(svc)
            session.commit()

        r = client.get("/maintenance/example-media")

    assert r.status_code == 200
    assert "Not managed" in r.text


def test_first_hit_triggers_a_wake_and_shows_progress(app_with_registry) -> None:
    from sqlmodel import Session, select

    with TestClient(app_with_registry) as client:
        r = client.get("/maintenance/example-media")
        assert r.status_code == 200
        assert "Waking" in r.text or "Ready" in r.text  # dry-run may finish very fast

        with Session(get_engine()) as session:
            runs = session.exec(select(PipelineRun)).all()
    assert len(runs) == 1


def test_a_second_hit_does_not_duplicate_the_wake(app_with_registry) -> None:
    from sqlmodel import Session, select

    with TestClient(app_with_registry) as client:
        client.get("/maintenance/example-media")
        client.get("/maintenance/example-media")

        with Session(get_engine()) as session:
            runs = session.exec(select(PipelineRun)).all()
    assert len(runs) == 1


def test_a_failed_wake_renders_the_failed_page(
    app_with_registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAKE_TIMEOUT_S", "0")
    config_module.reset_settings_cache()

    with TestClient(app_with_registry) as client:
        client.get("/maintenance/example-media")
        for _ in range(20):
            time.sleep(0.1)
            r = client.get("/maintenance/example-media")
            if "Waking" not in r.text:
                break

    assert "Could not start" in r.text
