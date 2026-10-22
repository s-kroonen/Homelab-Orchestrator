"""The service tree: parents, children, and what a sub-service inherits.

A sub-service (``Service.parent_id`` set) has no guest of its own; its guest and
node are its ROOT's. Everything that asks "what guest is this on?" goes through
:func:`effective_guest`. See docs/plan_service_tree.md.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from sqlmodel import Session, select

from orchestrator.db.models import Service
from orchestrator.domain.enums import GuestKind

#: Deepest nesting allowed (root = 1). Real trees are two levels; this only
#: stops a runaway or a cycle from walking forever.
MAX_DEPTH = 4


@dataclass(frozen=True)
class EffectiveGuest:
    kind: GuestKind
    guest_id: int | None
    node_id: int | None
    root_slug: str

    @property
    def has_guest(self) -> bool:
        return self.kind is not GuestKind.NONE and self.guest_id is not None


class TreeIndex:
    """The whole tree in memory (a few dozen services): one query, then walks."""

    def __init__(self, services: list[Service]) -> None:
        self.by_id: dict[int, Service] = {s.id: s for s in services if s.id is not None}
        self.by_slug: dict[str, Service] = {s.slug: s for s in services}
        self.children: dict[int, list[Service]] = {}
        for s in services:
            if s.parent_id is not None:
                self.children.setdefault(s.parent_id, []).append(s)
        for kids in self.children.values():
            kids.sort(key=lambda k: k.slug)

    @classmethod
    def build(cls, session: Session) -> TreeIndex:
        return cls(list(session.exec(select(Service)).all()))

    def parent(self, svc: Service) -> Service | None:
        return self.by_id.get(svc.parent_id) if svc.parent_id is not None else None

    def ancestors(self, svc: Service) -> list[Service]:
        """Nearest first; stops at MAX_DEPTH so a cycle cannot loop."""
        out: list[Service] = []
        cur = self.parent(svc)
        while cur is not None and len(out) < MAX_DEPTH:
            out.append(cur)
            cur = self.parent(cur)
        return out

    def root(self, svc: Service) -> Service:
        chain = self.ancestors(svc)
        return chain[-1] if chain else svc

    def depth(self, svc: Service) -> int:
        return len(self.ancestors(svc)) + 1

    def kids(self, svc: Service) -> list[Service]:
        return self.children.get(svc.id, []) if svc.id is not None else []

    def descendants(self, svc: Service) -> list[Service]:
        out: list[Service] = []
        stack = list(reversed(self.kids(svc)))
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(reversed(self.kids(cur)))
            if len(out) > 500:  # a cycle in hand-edited data
                break
        return out

    def walk(self) -> Iterator[tuple[Service, int]]:
        """Every service, parents before children, as ``(service, depth)``."""
        roots = sorted(
            (s for s in self.by_id.values() if self.parent(s) is None), key=lambda s: s.slug
        )
        for r in roots:
            yield r, 1
            for d in self.descendants(r):
                yield d, self.depth(d)

    def effective_guest(self, svc: Service) -> EffectiveGuest:
        root = self.root(svc)
        return EffectiveGuest(
            kind=root.guest_kind,
            guest_id=root.guest_id,
            node_id=root.node_id,
            root_slug=root.slug,
        )

    def is_served(self, svc: Service) -> bool:
        """Enabled itself and every ancestor enabled."""
        return svc.enabled and all(a.enabled for a in self.ancestors(svc))


# -- single-service conveniences (one query each; fine off the hot path) ------


def effective_guest(session: Session, svc: Service) -> EffectiveGuest:
    if svc.parent_id is None:
        return EffectiveGuest(svc.guest_kind, svc.guest_id, svc.node_id, svc.slug)
    return TreeIndex.build(session).effective_guest(svc)


def is_served(session: Session, svc: Service) -> bool:
    if svc.parent_id is None:
        return svc.enabled
    return TreeIndex.build(session).is_served(svc)


def would_cycle(index: TreeIndex, svc: Service, new_parent: Service) -> bool:
    """True if making ``new_parent`` the parent of ``svc`` puts svc above itself."""
    return new_parent.id == svc.id or any(d.id == new_parent.id for d in index.descendants(svc))
