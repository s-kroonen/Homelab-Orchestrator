"""Edit pages have both Save (stay) and Apply (save, then back to where you came from)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from orchestrator.db import session as session_module
from orchestrator.db.models import BackupPolicy, Node, Service
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind
from orchestrator.web.app import create_app
from tests.test_infra_pages import _login


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        node = Node(name="pve1", always_on=True)
        s.add(node)
        s.commit()
        s.refresh(node)
        s.add(Service(slug="media", name="Media", node_id=node.id, guest_kind=GuestKind.NONE))
        s.add(BackupPolicy(name="nightly"))
        s.commit()
    with TestClient(create_app()) as c:
        _login(c, monkeypatch)
        yield c


def test_service_page_edits_in_drawers(client: TestClient) -> None:
    assert client.get("/services/media").status_code == 200
    js = client.get("/static/service.js").text
    # One way to edit: each card's Edit opens a drawer; Save applies it (no Apply/Back pages).
    for fn in ("editGeneral", "editWhere", "editPower", "editBackup", "probeDrawer"):
        assert f"function {fn}" in js
    assert "PrevPage" not in js


@pytest.mark.parametrize("path", ["/backup-policies/new", "/backup-policies/nightly/edit"])
def test_form_pages_have_save_and_apply(client: TestClient, path: str) -> None:
    html = client.get(path).text
    assert 'id="submit-btn"' in html and 'id="apply-btn"' in html
    assert 'type="submit" id="apply-btn"' in html
    assert "PrevPage.go(" in html and 'id="cancel-link"' in html


def test_prev_page_helper_only_accepts_same_origin_paths(client: TestClient) -> None:
    js = client.get("/static/app.js").text
    assert "window.PrevPage" in js
    assert 'startsWith("/") && !path.startsWith("//")' in js
