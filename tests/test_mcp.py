"""The MCP server: the OAuth handshake Claude's connectors do, the JSON-RPC endpoint, scopes,
and the human approval a dangerous call waits for."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from orchestrator import config as config_module
from orchestrator.adapters.proxmox.base import Guest
from orchestrator.db import session as session_module
from orchestrator.db.models import AuditEntry, McpCall, McpGrant, PendingApproval
from orchestrator.db.session import get_engine
from orchestrator.domain.enums import GuestKind
from orchestrator.mcp import store
from orchestrator.registry.loader import reconcile_yaml_into_db
from orchestrator.web.app import create_app
from tests.test_proxy_hosts import _login_as_first_admin

REGISTRY = """
version: 1
nodes:
  - {name: pve1, always_on: true}
services:
  - slug: portainer-vm
    name: Portainer VM
    node: pve1
    guest_kind: vm
    guest_id: 101
  - slug: wiki
    name: Wiki
    parent: portainer-vm
"""

CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / "services.yaml"
    path.write_text(REGISTRY)
    monkeypatch.setenv("SERVICES_YAML_PATH", str(path))
    monkeypatch.setenv("EXTERNAL_URL", "https://orch.example.test")
    config_module.reset_settings_cache()
    session_module.create_all_for_tests()
    with Session(get_engine()) as s:
        reconcile_yaml_into_db(s, path)
        s.commit()
    with TestClient(create_app(), base_url="https://orch.example.test") as c:
        c.app.state.adapters.proxmox.simulated_guests = [  # type: ignore[attr-defined]
            Guest(node="pve1", vmid=101, kind=GuestKind.VM, name="p", status="running")
        ]
        _login_as_first_admin(c, monkeypatch)
        yield c


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


def connect(
    c: TestClient,
    scopes: list[str],
    *,
    confirm_writes: bool = False,
    redirect: str = CALLBACK,
) -> dict[str, Any]:
    """What Claude does: register, send the admin to /authorize, trade the code for tokens."""
    reg = c.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [redirect]})
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier, challenge = _pkce()
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": "https://orch.example.test/mcp",
        }
    )
    page = c.get(f"/oauth/authorize?{query}", follow_redirects=False)
    assert page.status_code == 200, page.text
    nonce = re.search(r'name="nonce" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    form = [
        ("nonce", nonce),
        ("client_id", client_id),
        ("redirect_uri", redirect),
        ("state", "xyz"),
        ("code_challenge", challenge),
        ("resource", "https://orch.example.test/mcp"),
        ("label", "my claude"),
        ("decision", "approve"),
        *[("scopes", s) for s in scopes],
    ]
    if confirm_writes:
        form.append(("confirm_writes", "on"))
    done = c.post(
        "/oauth/authorize",
        content=urlencode(form),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert done.status_code == 303, done.text
    target = urlsplit(done.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == redirect
    got = parse_qs(target.query)
    assert got["state"] == ["xyz"]
    token = c.post(
        "/oauth/token",
        content=urlencode(
            {
                "grant_type": "authorization_code",
                "code": got["code"][0],
                "client_id": client_id,
                "redirect_uri": redirect,
                "code_verifier": verifier,
                "resource": "https://orch.example.test/mcp",
            }
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert token.status_code == 200, token.text
    return {**token.json(), "client_id": client_id}


def rpc(
    c: TestClient, token: str, method: str, params: dict[str, Any] | None = None, id_: int = 1
) -> Any:
    r = c.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
        headers={"authorization": f"Bearer {token}"},
    )
    return r


def call(
    c: TestClient, token: str, name: str, args: dict[str, Any] | None = None
) -> dict[str, Any]:
    r = rpc(c, token, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


# -- discovery and the 401 -----------------------------------------------------------------------


def test_unauthenticated_is_a_401_pointing_at_the_metadata(client: TestClient) -> None:
    anon = TestClient(client.app, base_url="https://orch.example.test")
    r = anon.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    assert (
        'resource_metadata="https://orch.example.test/.well-known/oauth-protected-resource/mcp"'
        in r.headers["www-authenticate"]
    )
    assert anon.post("/mcp?access_token=x", json={}).status_code == 401  # never from the query


def test_metadata_matches_what_claude_checks(client: TestClient) -> None:
    res = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert res["resource"] == "https://orch.example.test/mcp"
    assert res["authorization_servers"] == ["https://orch.example.test"]
    assert client.get("/.well-known/oauth-protected-resource").json() == res
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["client_id_metadata_document_supported"] is True
    assert "none" in meta["token_endpoint_auth_methods_supported"]
    assert meta["registration_endpoint"].endswith("/oauth/register")
    assert "offline_access" in meta["scopes_supported"]


def test_get_mcp_is_405_not_404(client: TestClient) -> None:
    assert client.get("/mcp").status_code == 405


def test_switched_off_is_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_ENABLED", "false")
    config_module.reset_settings_cache()
    assert client.get("/.well-known/oauth-authorization-server").status_code == 404
    assert client.post("/mcp", json={}).status_code == 404


# -- registration and consent ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    ["http://evil.example/cb", "javascript:alert(1)", "not a url", "https://x.test/cb#frag"],
)
def test_registration_rejects_unsafe_redirect_uris(client: TestClient, uri: str) -> None:
    r = client.post("/oauth/register", json={"client_name": "x", "redirect_uris": [uri]})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


def test_loopback_redirect_matches_on_any_port(client: TestClient) -> None:
    got = connect(client, ["read"], redirect="http://localhost/callback")
    assert got["access_token"]
    assert store.redirect_matches(["http://localhost/callback"], "http://127.0.0.1:53124/callback")
    assert not store.redirect_matches(["http://localhost/callback"], "http://localhost:1/other")
    assert not store.redirect_matches([CALLBACK], "https://evil.example/cb")


def test_authorize_needs_login_and_shows_where_it_goes(client: TestClient) -> None:
    anon = TestClient(client.app, base_url="https://orch.example.test")
    reg = anon.post(
        "/oauth/register", json={"client_name": "C", "redirect_uris": [CALLBACK]}
    ).json()
    q = urlencode(
        {
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": CALLBACK,
            "code_challenge": _pkce()[1],
            "code_challenge_method": "S256",
        }
    )
    r = anon.get(f"/oauth/authorize?{q}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    page = client.get(f"/oauth/authorize?{q}")
    assert "claude.ai" in page.text and "Allow" in page.text


def test_authorize_refuses_a_foreign_redirect_without_redirecting(client: TestClient) -> None:
    reg = client.post(
        "/oauth/register", json={"client_name": "C", "redirect_uris": [CALLBACK]}
    ).json()
    q = urlencode(
        {
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "https://evil.example/cb",
            "code_challenge": _pkce()[1],
            "code_challenge_method": "S256",
        }
    )
    r = client.get(f"/oauth/authorize?{q}", follow_redirects=False)
    assert r.status_code == 400


def test_pkce_is_required(client: TestClient) -> None:
    reg = client.post(
        "/oauth/register", json={"client_name": "C", "redirect_uris": [CALLBACK]}
    ).json()
    q = urlencode(
        {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": CALLBACK}
    )
    r = client.get(f"/oauth/authorize?{q}", follow_redirects=False)
    assert r.status_code == 303 and "error=invalid_request" in r.headers["location"]


def test_denying_sends_access_denied(client: TestClient) -> None:
    reg = client.post(
        "/oauth/register", json={"client_name": "C", "redirect_uris": [CALLBACK]}
    ).json()
    _, challenge = _pkce()
    q = urlencode(
        {
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": CALLBACK,
            "state": "s1",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    page = client.get(f"/oauth/authorize?{q}")
    nonce = re.search(r'name="nonce" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    r = client.post(
        "/oauth/authorize",
        content=urlencode(
            {
                "nonce": nonce,
                "client_id": reg["client_id"],
                "redirect_uri": CALLBACK,
                "state": "s1",
                "code_challenge": challenge,
                "decision": "deny",
            }
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=access_denied" in r.headers["location"] and "state=s1" in r.headers["location"]


# -- tokens ----------------------------------------------------------------------------------------


def _token(c: TestClient, **form: str) -> Any:
    return c.post(
        "/oauth/token",
        content=urlencode(form),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


def test_code_is_single_use_and_bound_to_pkce(client: TestClient) -> None:
    reg = client.post(
        "/oauth/register", json={"client_name": "C", "redirect_uris": [CALLBACK]}
    ).json()
    verifier, challenge = _pkce()
    with Session(get_engine()) as s:
        code = store.issue_code(
            s,
            client_id=reg["client_id"],
            redirect_uri=CALLBACK,
            code_challenge=challenge,
            scopes=["read"],
            role="viewer",
            label="x",
            confirm_writes=False,
            admin_username="storm",
            resource="",
        )
        s.commit()
    base = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": reg["client_id"],
        "redirect_uri": CALLBACK,
    }
    assert _token(client, **base, code_verifier="w" * 50).json()["error"] == "invalid_grant"
    assert _token(client, **base, code_verifier=verifier).status_code == 200
    assert _token(client, **base, code_verifier=verifier).json()["error"] == "invalid_grant"


def test_unknown_client_is_401_invalid_client(client: TestClient) -> None:
    r = _token(client, grant_type="refresh_token", refresh_token="x", client_id="gone")
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"


def test_refresh_rotates_and_a_replayed_token_kills_the_connection(client: TestClient) -> None:
    got = connect(client, ["read"])
    first = _token(
        client,
        grant_type="refresh_token",
        refresh_token=got["refresh_token"],
        client_id=got["client_id"],
    )
    assert first.status_code == 200
    new = first.json()
    assert new["refresh_token"] != got["refresh_token"] and new["scope"] == "read"
    assert rpc(client, new["access_token"], "ping").status_code == 200
    replay = _token(
        client,
        grant_type="refresh_token",
        refresh_token=got["refresh_token"],
        client_id=got["client_id"],
    )
    assert replay.json()["error"] == "invalid_grant"
    assert rpc(client, new["access_token"], "ping").status_code == 401  # the copy leaked: dropped


def test_revoking_stops_the_token_at_once(client: TestClient) -> None:
    got = connect(client, ["read"])
    assert rpc(client, got["access_token"], "ping").status_code == 200
    grants = client.get("/api/assistants/grants").json()
    assert (
        client.post(f"/api/assistants/grants/{grants[0]['id']}/revoke").json()["state"] == "revoked"
    )
    assert rpc(client, got["access_token"], "ping").status_code == 401
    refresh = _token(
        client,
        grant_type="refresh_token",
        refresh_token=got["refresh_token"],
        client_id=got["client_id"],
    )
    assert refresh.json()["error"] == "invalid_grant"


def test_access_token_expires(client: TestClient) -> None:
    got = connect(client, ["read"])
    with Session(get_engine()) as s:
        g = s.exec(select(McpGrant)).one()
        g.access_expires_at = store.now() - store.timedelta(seconds=1)
        s.add(g)
        s.commit()
    assert rpc(client, got["access_token"], "ping").status_code == 401


# -- the protocol and the tools --------------------------------------------------------------------


def test_initialize_and_list_follow_the_scopes(client: TestClient) -> None:
    got = connect(client, ["read"])
    init = rpc(client, got["access_token"], "initialize", {"protocolVersion": "2025-06-18"}).json()
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "homelab-orchestrator"
    names = {
        t["name"] for t in rpc(client, got["access_token"], "tools/list").json()["result"]["tools"]
    }
    assert "list_services" in names and "run_backup" not in names and "delete_stack" not in names
    note = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"authorization": f"Bearer {got['access_token']}"},
    )
    assert note.status_code == 202


def test_read_tools_return_the_estate(client: TestClient) -> None:
    got = connect(client, ["read"])
    out = call(client, got["access_token"], "list_services")
    assert out["isError"] is False
    assert "portainer-vm" in out["content"][0]["text"] and "wiki" in out["content"][0]["text"]
    status = call(client, got["access_token"], "orchestrator_status")
    assert '"services": 2' in status["content"][0]["text"]


def test_a_tool_without_its_scope_is_refused_and_logged(client: TestClient) -> None:
    got = connect(client, ["read"])
    out = call(client, got["access_token"], "run_backup", {"slug": "portainer-vm"})
    assert out["isError"] is True and "backup" in out["content"][0]["text"]
    with Session(get_engine()) as s:
        row = s.exec(select(McpCall).where(McpCall.tool == "run_backup")).one()
        assert row.status == "refused"


def test_bad_arguments_are_an_error_result(client: TestClient) -> None:
    got = connect(client, ["read"])
    assert call(client, got["access_token"], "get_service", {})["isError"] is True
    assert call(client, got["access_token"], "get_service", {"slug": "x", "nope": 1})["isError"]
    assert rpc(client, got["access_token"], "tools/call", {"name": "nope"}).json()["error"]


def test_an_act_tool_runs_at_once_and_is_audited_as_the_connection(client: TestClient) -> None:
    got = connect(client, ["read", "wake"])
    out = call(client, got["access_token"], "wake_service", {"slug": "wiki"})
    assert out["isError"] is False, out
    with Session(get_engine()) as s:
        calls = s.exec(select(McpCall).where(McpCall.tool == "wake_service")).all()
        assert [c.status for c in calls] == ["ok"] and calls[0].label == "my claude"
        actors = {a.actor for a in s.exec(select(AuditEntry)).all()}
        assert any(a.startswith("mcp:my claude") for a in actors)


def test_backup_starts_and_reports(client: TestClient) -> None:
    got = connect(client, ["read", "backup"])
    out = call(client, got["access_token"], "run_backup", {"slug": "wiki"})
    assert "content" in out  # ran, or started - either way not a crash


# -- dangerous: a person must say yes ----------------------------------------------------------------


def test_dangerous_without_propose_is_refused(client: TestClient) -> None:
    got = connect(client, ["read", "control"])
    out = call(
        client, got["access_token"], "guest_power", {"node": "pve1", "vmid": 101, "action": "stop"}
    )
    assert out["isError"] is True and "propose" in out["content"][0]["text"]
    names = {
        t["name"] for t in rpc(client, got["access_token"], "tools/list").json()["result"]["tools"]
    }
    assert "delete_stack" not in names and "guest_power" in names


def _only_request(c: TestClient) -> dict[str, Any]:
    rows = c.get("/api/assistants/requests").json()
    assert len(rows) == 1
    return rows[0]


def test_dangerous_waits_then_runs_only_after_approval(client: TestClient) -> None:
    got = connect(client, ["read", "control", "propose"])
    pve = client.app.state.adapters.proxmox  # type: ignore[attr-defined]
    real_stop, stopped = pve.stop_guest, []

    async def spy(node: str, vmid: int, kind: GuestKind) -> Any:
        stopped.append(vmid)
        return await real_stop(node, vmid, kind)

    pve.stop_guest = spy
    args = {"node": "pve1", "vmid": 101, "action": "stop"}
    out = call(client, got["access_token"], "guest_power", args)
    text = out["content"][0]["text"]
    assert out["isError"] is False and "NOT DONE" in text
    request = _only_request(client)
    assert request["status"] == "waiting"
    assert request["url"] in text
    assert stopped == []  # asked, not done
    still = call(client, got["access_token"], "get_request", {"request_id": request["request_id"]})
    assert '"status": "waiting"' in still["content"][0]["text"]

    done = client.post(f"/api/assistants/requests/{request['request_id']}/approve")
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "approved" and body["decided_by"] == "storm"
    assert body["result"]["ok"] is True, body["result"]
    assert stopped == [101]  # ran once, after the yes
    with Session(get_engine()) as s:
        row = s.exec(select(McpCall).where(McpCall.tool == "guest_power")).one()
        assert row.status == "ok" and row.decided_by == "storm"
        actions = [a.action for a in s.exec(select(AuditEntry)).all()]
        assert "mcp.requested" in actions and "mcp.approved" in actions
    again = client.post(f"/api/assistants/requests/{request['request_id']}/approve")
    assert again.status_code == 409  # one approval, one run


def test_a_denied_request_never_runs(client: TestClient) -> None:
    got = connect(client, ["read", "control", "propose"])
    call(
        client, got["access_token"], "guest_power", {"node": "pve1", "vmid": 101, "action": "stop"}
    )
    request = _only_request(client)
    denied = client.post(f"/api/assistants/requests/{request['request_id']}/deny").json()
    assert denied["status"] == "denied"
    assert (
        client.post(f"/api/assistants/requests/{request['request_id']}/approve").status_code == 409
    )
    with Session(get_engine()) as s:
        assert s.exec(select(McpCall).where(McpCall.tool == "guest_power")).one().status == "denied"


def test_an_expired_request_cannot_be_approved(client: TestClient) -> None:
    got = connect(client, ["read", "control", "propose"])
    call(
        client, got["access_token"], "guest_power", {"node": "pve1", "vmid": 101, "action": "stop"}
    )
    token = _only_request(client)["request_id"]
    with Session(get_engine()) as s:
        row = s.exec(select(PendingApproval)).one()
        row.expires_at = store.now() - store.timedelta(minutes=1)
        s.add(row)
        s.commit()
    assert client.post(f"/api/assistants/requests/{token}/approve").status_code == 409


def test_revoking_the_connection_cancels_its_waiting_requests(client: TestClient) -> None:
    got = connect(client, ["read", "control", "propose"])
    call(
        client, got["access_token"], "guest_power", {"node": "pve1", "vmid": 101, "action": "stop"}
    )
    token = _only_request(client)["request_id"]
    gid = client.get("/api/assistants/grants").json()[0]["id"]
    client.post(f"/api/assistants/grants/{gid}/revoke")
    assert client.post(f"/api/assistants/requests/{token}/approve").status_code == 409


def test_unguarded_deploy_is_dangerous_and_a_guarded_one_is_not(client: TestClient) -> None:
    got = connect(client, ["read", "deploy", "propose"])
    out = call(
        client,
        got["access_token"],
        "deploy_service",
        {"slug": "wiki", "skip_guards": True, "reason": "x"},
    )
    assert "NOT DONE" in out["content"][0]["text"]
    guarded = call(
        client, got["access_token"], "deploy_service", {"slug": "wiki", "guards_only": True}
    )
    assert (
        "NOT DONE" not in guarded["content"][0]["text"]
    )  # refused for another reason, but not parked


def test_ask_before_every_change_parks_even_a_safe_action(client: TestClient) -> None:
    got = connect(client, ["read", "wake"], confirm_writes=True)
    out = call(client, got["access_token"], "wake_service", {"slug": "wiki"})
    assert "NOT DONE" in out["content"][0]["text"]
    assert call(client, got["access_token"], "list_services")["isError"] is False  # reads are free


def test_one_connection_cannot_read_anothers_request(client: TestClient) -> None:
    a = connect(client, ["read", "control", "propose"])
    call(client, a["access_token"], "guest_power", {"node": "pve1", "vmid": 101, "action": "stop"})
    token = _only_request(client)["request_id"]
    b = connect(client, ["read"])
    out = call(client, b["access_token"], "get_request", {"request_id": token})
    assert out["isError"] is True


# -- history, personal tokens, editing a connection ----------------------------------------------------


def test_history_lists_calls_with_redacted_args(client: TestClient) -> None:
    got = connect(client, ["read"])
    call(client, got["access_token"], "get_service", {"slug": "wiki"})
    rows = client.get("/api/assistants/history").json()
    assert rows[0]["tool"] == "get_service" and rows[0]["args"] == {"slug": "wiki"}
    assert rows[0]["label"] == "my claude" and rows[0]["risk"] == "read"
    assert store.redact({"password": "p", "nested": {"api_token": "t", "ok": 1}}) == {
        "password": "***",
        "nested": {"api_token": "***", "ok": 1},
    }
    assert client.get("/api/assistants/history?status=refused").json() == []


def test_personal_token_has_its_own_role_and_works_without_oauth(client: TestClient) -> None:
    made = client.post("/api/assistants/grants", json={"label": "ci", "role": "operator"})
    assert made.status_code == 201
    token = made.json()["token"]
    assert made.json()["scopes"] == ["read", "backup", "wake"]
    assert call(client, token, "list_services")["isError"] is False
    names = {t["name"] for t in rpc(client, token, "tools/list").json()["result"]["tools"]}
    assert "run_backup" in names and "deploy_service" not in names
    assert client.post("/api/assistants/grants", json={"label": "x"}).status_code == 400
    assert (
        client.post("/api/assistants/grants", json={"label": "x", "scopes": ["root"]}).status_code
        == 400
    )


def test_changing_a_role_takes_effect_on_the_next_call(client: TestClient) -> None:
    got = connect(client, ["read", "wake"])
    gid = client.get("/api/assistants/grants").json()[0]["id"]
    assert call(client, got["access_token"], "wake_service", {"slug": "wiki"})["isError"] is False
    r = client.patch(f"/api/assistants/grants/{gid}", json={"role": "viewer"})
    assert r.json()["role"] == "viewer"
    assert call(client, got["access_token"], "wake_service", {"slug": "wiki"})["isError"] is True


def test_tokens_are_stored_hashed(client: TestClient) -> None:
    got = connect(client, ["read"])
    with Session(get_engine()) as s:
        g = s.exec(select(McpGrant)).one()
        assert got["access_token"] not in (g.access_hash, g.refresh_hash)
        assert g.access_hash == store.sha256(got["access_token"])


def test_the_api_and_pages_need_an_admin(client: TestClient) -> None:
    anon = TestClient(client.app, base_url="https://orch.example.test")
    assert anon.get("/api/assistants/grants").status_code == 401
    assert anon.get("/assistants", follow_redirects=False).status_code == 303
    assert client.get("/assistants").status_code == 200


# -- client metadata documents (what Claude Code uses) --------------------------------------------------

CIMD_ID = "https://claude.ai/oauth/claude-code-client-metadata"


def test_a_metadata_document_client_can_connect(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator.mcp import cimd

    async def fake(settings: Any, client_id: str) -> dict[str, Any]:
        assert client_id == CIMD_ID
        return {
            "client_id": CIMD_ID,
            "client_name": "Claude Code",
            "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
        }

    monkeypatch.setattr(cimd, "fetch_client_metadata", fake)
    verifier, challenge = _pkce()
    q = urlencode(
        {
            "response_type": "code",
            "client_id": CIMD_ID,
            "redirect_uri": "http://127.0.0.1:40123/callback",
            "state": "s",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    page = client.get(f"/oauth/authorize?{q}")
    assert page.status_code == 200 and "Claude Code" in page.text
    assert "on the device that is connecting" in page.text  # loopback gets the warning
    with Session(get_engine()) as s:
        row = store.get_client(s, CIMD_ID)
        assert row is not None and row.kind == "cimd"


def test_metadata_documents_are_only_fetched_from_trusted_hosts(client: TestClient) -> None:
    import asyncio

    from orchestrator.mcp import cimd
    from orchestrator.mcp.store import OAuthError

    settings = config_module.get_settings()
    for bad in ("https://evil.example/client.json", "https://169.254.169.254/x", "https://claude.ai/"):
        with pytest.raises(OAuthError):
            asyncio.run(cimd.fetch_client_metadata(settings, bad))


# -- the shell ---------------------------------------------------------------------------------------


def test_cli_makes_lists_and_revokes_a_token(client: TestClient) -> None:
    from click.testing import CliRunner

    from orchestrator.cli import cli

    runner = CliRunner()
    made = runner.invoke(cli, ["mcp", "token", "ci", "--role", "operator"])
    assert made.exit_code == 0, made.output
    token = next(line for line in made.output.splitlines() if line.startswith("mcp_pt_"))
    assert call(client, token, "list_services")["isError"] is False
    listed = runner.invoke(cli, ["mcp", "grants"])
    assert "ci" in listed.output and "operator" in listed.output
    assert "list_services" in runner.invoke(cli, ["mcp", "history"]).output
    gid = client.get("/api/assistants/grants").json()[0]["id"]
    assert runner.invoke(cli, ["mcp", "revoke", str(gid)]).exit_code == 0
    assert rpc(client, token, "ping").status_code == 401


def test_cli_approves_a_waiting_request(client: TestClient) -> None:
    from click.testing import CliRunner

    from orchestrator.cli import cli

    got = connect(client, ["read", "control", "propose"])
    call(client, got["access_token"], "guest_power", {"node": "pve1", "vmid": 101, "action": "stop"})
    token = _only_request(client)["request_id"]
    runner = CliRunner()
    assert token in runner.invoke(cli, ["mcp", "requests"]).output
    done = runner.invoke(cli, ["mcp", "approve", token])
    assert done.exit_code == 0, done.output
    assert client.get(f"/api/assistants/requests/{token}").json()["status"] == "approved"
