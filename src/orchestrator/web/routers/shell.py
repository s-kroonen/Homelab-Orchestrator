"""What the frame around every page needs: the pending pill and the global search.

* ``GET  /api/pending``        what is saved but not written to services.yaml or pushed yet
                               (registry/autosave.py). Answers from memory, contacts nothing.
* ``POST /api/pending/retry``  write and push now (the pill's Retry; also "Save" when autosave is
                               off).
* ``GET  /api/search``         everything the header search can jump to: services, nodes, proxy
                               hosts and pages. The browser filters; the list is small.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlmodel import select

from orchestrator.db.models import Node, ProxyHost, Service
from orchestrator.db.session import session_scope
from orchestrator.registry.autosave import Autosaver
from orchestrator.registry.loader import compute_registry_diff
from orchestrator.web.auth import CurrentAdmin, require_admin
from orchestrator.web.deps import AdaptersDep, SessionDep, SettingsDep

router = APIRouter(prefix="/api", tags=["shell"], dependencies=[Depends(require_admin)])

#: (label, href, words that also find it) for the page part of the search.
PAGES: list[tuple[str, str, str]] = [
    ("Dashboard", "/", "home overview"),
    ("Map", "/map", "graph path"),
    ("Services", "/services", "list"),
    ("Nodes & guests", "/nodes", "proxmox vm lxc guests power"),
    ("Proxy hosts", "/proxy-hosts", "traefik routes domains"),
    ("Containers", "/portainer", "portainer stacks docker"),
    ("Backups", "/backup-policies", "pbs policies snapshots dumps"),
    ("Activity", "/activity", "runs log audit"),
    ("Traffic", "/traffic", "requests countries geo"),
    ("Connections", "/infra/connections", "adapters settings connectivity"),
    ("Notifications", "/notifications", "alerts push ntfy"),
    ("AI assistants", "/assistants", "mcp claude"),
    ("Admins & devices", "/admins", "users passkeys invite"),
    ("System", "/system", "update version timezone backups"),
    ("Archive", "/archive", "deleted restore"),
]


def _autosaver(request: Request, settings: SettingsDep, adapters: AdaptersDep) -> Autosaver:
    saver = getattr(request.app.state, "autosaver", None)
    if saver is None:  # an app built without the lifespan (some tests)
        saver = Autosaver(settings=settings, adapters=adapters)
        request.app.state.autosaver = saver
    return saver


def pending_view(saver: Autosaver, yaml_dirty: bool) -> dict[str, Any]:
    st = saver.state
    items: list[str] = []
    if st.save_error:
        items.append(st.save_error)
    elif yaml_dirty and not (saver.enabled and st.settling()):
        items.append("Changes not written to services.yaml yet")
    for f in st.push_failed:
        host = f.get("hostname") or "*"
        items.append(
            f"{host}: {f.get('message') or 'push failed'}" if host != "*" else str(f.get("message"))
        )
    pending_push = st.push_pending or 0
    if pending_push and not st.push_failed:
        items.append(
            f"{pending_push} proxy host change{'s' if pending_push != 1 else ''} "
            "not pushed to Traefik Manager"
        )
    return {
        "autosave": saver.enabled,
        "yaml_dirty": yaml_dirty,
        "count": len(items),
        "items": items,
        **st.as_dict(),
        "running": st.running or (saver.enabled and yaml_dirty and st.settling()),
    }


@router.get("/pending")
async def get_pending(
    session: SessionDep, saver: Autosaver = Depends(_autosaver)  # noqa: B008
) -> dict[str, Any]:
    return pending_view(saver, compute_registry_diff(session).dirty)


@router.post("/pending/retry")
async def retry_pending(
    admin: CurrentAdmin, saver: Autosaver = Depends(_autosaver)  # noqa: B008
) -> dict[str, Any]:
    await saver.run_once(actor=admin.username)
    with session_scope() as session:
        dirty = compute_registry_diff(session).dirty
    return pending_view(saver, dirty)


@router.get("/search")
async def search_index(session: SessionDep) -> dict[str, Any]:
    items: list[dict[str, str]] = []
    nodes = {n.id: n.name for n in session.exec(select(Node)).all()}
    for s in session.exec(select(Service).order_by(Service.slug)).all():
        items.append(
            {
                "kind": "service",
                "label": s.name or s.slug,
                "sub": " · ".join(x for x in (s.slug, s.group, nodes.get(s.node_id, "")) if x),
                "href": f"/services/{s.slug}",
            }
        )
    for name in sorted(nodes.values()):
        items.append({"kind": "node", "label": name, "sub": "", "href": f"/nodes?q={name}"})
    for ph in session.exec(select(ProxyHost).order_by(ProxyHost.hostname)).all():
        items.append(
            {
                "kind": "proxy host",
                "label": ph.hostname,
                "sub": ph.upstream or "",
                "href": f"/proxy-hosts?q={ph.hostname}",
            }
        )
    for label, href, words in PAGES:
        items.append({"kind": "page", "label": label, "sub": words, "href": href})
    return {"items": items}
