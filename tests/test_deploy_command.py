"""The deploy command: a service's deploy, with its key, to paste somewhere else."""

from __future__ import annotations

import re
from typing import Any

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator.config import get_settings
from orchestrator.db.models import AuditEntry
from orchestrator.db.session import get_engine
from orchestrator.mcp.tools import TOOLS, ToolContext
from tests.test_deploy_webhook import (  # noqa: F401 - fixtures
    fake_health,
    generic,
    new_secret,
    wait_run,
)

URL = "/api/services/dev-xymbols/deploy/command"


def _actions() -> list[str]:
    with Session(get_engine()) as s:
        return [a.action for a in s.exec(select(AuditEntry).order_by(AuditEntry.id)).all()]


def test_no_webhook_or_no_key_is_refused(generic: TestClient) -> None:  # noqa: F811
    assert generic.post("/api/services/media/deploy/command").status_code == 409  # no webhook
    r = generic.post(URL)
    assert r.status_code == 409 and "no deploy key yet" in r.json()["detail"]
    assert "deploy.key_revealed" not in _actions()


def test_the_command_carries_the_key_and_deploys(generic: TestClient) -> None:  # noqa: F811
    key = new_secret(generic)
    r = generic.post(URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key"] == key and body["created"] is False and body["branch"] == "dev"
    assert body["url"] == "https://orch.example.lan/hooks/deploy/dev-xymbols"
    deploy = body["commands"]["deploy"]
    assert deploy == (
        f'curl -fsS -X POST -H "Authorization: Bearer {key}" '
        "https://orch.example.lan/hooks/deploy/dev-xymbols"
    )
    assert body["commands"]["status"].endswith("/hooks/deploy/dev-xymbols/runs/RUN_ID")
    assert key in body["github_inline_key"] and body["secret_name"] == "DEPLOY_KEY_DEV_XYMBOLS"
    assert _actions().count("deploy.key_revealed") == 1

    # Running what was shown works: the key in it is the webhook's.
    shown = re.search(r"Bearer (\S+)\"", deploy)
    assert shown is not None
    started = generic.post(
        "/hooks/deploy/dev-xymbols", headers={"Authorization": f"Bearer {shown.group(1)}"}
    )
    assert started.status_code == 202, started.text
    run = wait_run(generic, started.json()["run_id"])
    assert run["status"] == "succeeded"


def test_create_makes_a_key_only_when_there_is_none(generic: TestClient) -> None:  # noqa: F811
    first = generic.post(URL, json={"create": True}).json()
    assert first["created"] is True and first["key"]
    again = generic.post(URL, json={"create": True}).json()
    assert again["created"] is False and again["key"] == first["key"]  # never replaced here
    acts = _actions()
    assert acts.count("deploy.webhook_secret") == 1 and acts.count("deploy.key_revealed") == 2


async def test_the_assistant_gets_it_with_the_configure_scope(
    generic: TestClient,  # noqa: F811
) -> None:
    tool = TOOLS["deploy_command"]
    assert tool.scope == "configure" and tool.writes
    with Session(get_engine()) as s:
        ctx = ToolContext(session=s, adapters=None, settings=get_settings(), actor="mcp:test")
        out: dict[str, Any] = await tool.run(
            ctx, {"slug": "dev-xymbols", "create_if_missing": True}
        )
    assert out["created"] is True and out["key"] in out["commands"]["deploy"]
    assert "github_inline_key" not in out and "run_id" in out["next"]
    with Session(get_engine()) as s:
        entry = s.exec(select(AuditEntry).where(AuditEntry.action == "deploy.key_revealed")).one()
    assert entry.actor == "mcp:test"
