"""``orchestrator-cli mcp`` - the AI-assistant connections, tokens, history and requests from the
shell. The same data and rules as the /assistants page (docs/mcp.md)."""

from __future__ import annotations

import asyncio
import json

import click
from sqlmodel import Session, col, select

from orchestrator.adapters.factory import build_adapters
from orchestrator.config import get_settings
from orchestrator.db.models import McpGrant
from orchestrator.db.session import build_engine, get_engine
from orchestrator.logging_setup import configure_logging
from orchestrator.mcp import approvals, store
from orchestrator.mcp.scopes import ROLE_BLURBS, ROLES, SCOPES, parse_scope, role_of
from orchestrator.mcp.tools import TOOLS, ToolError


def _bootstrap() -> None:
    configure_logging()
    build_engine()


def _when(iso: str | None) -> str:
    return iso[:19].replace("T", " ") if iso else "never"


@click.group()
def mcp() -> None:
    """AI assistants (MCP): connections, tokens, history, requests that need a person."""


@mcp.command("status")
def status() -> None:
    """The address to give an assistant, and what is allowed."""
    settings = get_settings()
    click.echo(f"enabled   : {'yes' if settings.mcp_enabled else 'NO (MCP_ENABLED=false)'}")
    click.echo(f"address   : {store.mcp_url(settings)}")
    click.echo(f"sign-in   : {store.origin(settings)}/.well-known/oauth-authorization-server")
    click.echo("roles:")
    for name, scopes in ROLES.items():
        click.echo(f"  {name:<9} {', '.join(scopes):<42} {ROLE_BLURBS[name]}")
    click.echo(f"tools     : {len(TOOLS)} (orchestrator-cli mcp tools)")


@mcp.command("tools")
def tools() -> None:
    """Every tool, what it needs and how risky it is."""
    for t in TOOLS.values():
        risk = "dangerous" if t.risk == "dangerous" else "read" if t.risk == "read" else "act"
        if callable(t.risk):
            risk = "act / dangerous"
        click.echo(f"{t.name:<24} needs {t.scope:<8} {risk}")


@mcp.command("grants")
@click.option("--all", "show_all", is_flag=True, help="Include revoked and expired ones.")
def grants(show_all: bool) -> None:
    """Connections and personal tokens."""
    _bootstrap()
    with Session(get_engine()) as session:
        rows = session.exec(select(McpGrant).order_by(col(McpGrant.created_at).desc())).all()
        views = [store.grant_view(g) for g in rows]
    shown = [v for v in views if show_all or v["state"] == "active"]
    if not shown:
        click.echo(
            "No connections." if not views else "No active connections (--all shows the rest)."
        )
        return
    for v in shown:
        click.echo(
            f"#{v['id']:<3} {v['label']:<24} {v['kind']:<9} {v['role']:<9} "
            f"{','.join(v['scopes']):<34} last used {_when(v['last_used_at'])}  {v['state']}"
            + ("  ask-first" if v["confirm_writes"] else "")
        )


@mcp.command("token")
@click.argument("label")
@click.option("--role", type=click.Choice(list(ROLES)), default=None)
@click.option("--scope", "scopes", multiple=True, type=click.Choice(list(SCOPES)))
@click.option("--ask-first", is_flag=True, help="A person approves every change it makes.")
@click.option("--days", type=int, default=None, help="Expire after this many days.")
def token(
    label: str, role: str | None, scopes: tuple[str, ...], ask_first: bool, days: int | None
) -> None:
    """Make a personal token (shown once) for a client that cannot do OAuth."""
    chosen = parse_scope(list(scopes)) or list(ROLES.get(role or "viewer", ()))
    _bootstrap()
    settings = get_settings()
    with Session(get_engine()) as session:
        grant, secret = store.create_personal(
            session,
            settings,
            label=label,
            scopes=chosen,
            admin_username="cli",
            confirm_writes=ask_first,
            expires_days=days,
        )
        click.echo(f"Created #{grant.id} {grant.label} ({role_of(chosen)}: {', '.join(chosen)})")
    click.echo("Shown once - copy it now:")
    click.echo(secret)
    click.echo(f"Use: Authorization: Bearer <token>   at {store.mcp_url(settings)}")


@mcp.command("revoke")
@click.argument("grant_id", type=int)
def revoke(grant_id: int) -> None:
    """Cut a connection off at once."""
    _bootstrap()
    with Session(get_engine()) as session:
        grant = store.revoke_grant(session, grant_id)
        if grant is None:
            raise click.ClickException(f"no connection #{grant_id}")
        click.echo(f"Revoked #{grant_id} {grant.label}.")


@mcp.command("history")
@click.option("--limit", type=int, default=30, show_default=True)
@click.option("--tool")
@click.option("--status", "status_")
@click.option("--risk", type=click.Choice(["read", "act", "dangerous"]))
@click.option("--grant", "grant_id", type=int)
@click.option("--json", "as_json", is_flag=True)
def history(
    limit: int,
    tool: str | None,
    status_: str | None,
    risk: str | None,
    grant_id: int | None,
    as_json: bool,
) -> None:
    """What the assistants did, newest first."""
    _bootstrap()
    with Session(get_engine()) as session:
        rows = store.history(
            session, limit=limit, grant_id=grant_id, tool=tool, status=status_, risk=risk
        )
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        args = json.dumps(r["args"]) if r["args"] else ""
        click.echo(
            f"{_when(r['at'])}  {r['label']:<16} {r['tool']:<22} {r['risk']:<9} "
            f"{r['status']:<8} {r['summary']}  {args}"
        )
    if not rows:
        click.echo("No calls.")


@mcp.command("requests")
def requests() -> None:
    """Dangerous requests: waiting, and the recent ones."""
    _bootstrap()
    settings = get_settings()
    with Session(get_engine()) as session:
        rows = [
            approvals.request_view(session, settings, r.token) for r in approvals.waiting(session)
        ]
    if not rows:
        click.echo("Nothing has asked for approval.")
    for r in rows:
        click.echo(f"{r['status']:<9} {r['connection']:<16} {r['summary']}")
        click.echo(f"          id {r['request_id']}")


def _row(session: Session, token: str):  # type: ignore[no-untyped-def]
    row = approvals.find(session, token)
    if row is None:
        raise click.ClickException("no such request")
    return row


@mcp.command("deny")
@click.argument("request_id")
def deny(request_id: str) -> None:
    """Say no to a waiting request."""
    _bootstrap()
    with Session(get_engine()) as session:
        try:
            approvals.deny(session, _row(session, request_id), "cli")
        except ToolError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo("Denied.")


@mcp.command("approve")
@click.argument("request_id")
def approve(request_id: str) -> None:
    """Say yes to a waiting request and run it now."""
    _bootstrap()
    settings = get_settings()
    adapters = build_adapters(settings)

    async def go() -> dict:  # type: ignore[type-arg]
        await adapters.start_all()
        try:
            with Session(get_engine()) as session:
                row = _row(session, request_id)
                if row.payload.get("tool") == "deploy_service":
                    raise click.ClickException(
                        "approve deploys from the dashboard: they keep running in the server"
                    )
                await approvals.approve(session, adapters, settings, row, "cli")
                return dict(row.payload.get("result") or {})
        finally:
            await adapters.stop_all()

    try:
        result = asyncio.run(go())
    except ToolError as exc:
        raise click.ClickException(str(exc)) from exc
    if result.get("ok"):
        click.secho("Approved and done.", fg="green")
        click.echo(result.get("output", ""))
    else:
        click.secho(f"Approved, but it failed: {result.get('error')}", fg="red")
        raise SystemExit(1)
