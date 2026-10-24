"""What a backup needs awake before it can start — resolved, validated, previewed.

Two inputs say what must be up:

* **prerequisites** — ``BackupPolicy.requires`` (everything the policy backs up
  needs them, e.g. the storage VM hosting the PBS datastore's NFS) and
  ``Service.backup_requires`` (extras of one service, e.g. its database), and
* **the target** itself, when the policy has ``wake_before_backup`` on.

:func:`resolve_backup_plan` is the single place that order is decided. The
backup pipeline executes it, ``GET /api/backup/plan`` and ``orchestrator-cli
backup plan`` preview it, and the service page renders it — so the preview can
never drift from what a run would do.

``requires`` is deliberately NOT ``Service.depends_on``: that one is a
health-gate dependency (a down gateway makes a verdict UNKNOWN rather than
FAILED) and never wakes anything. And it is NOT transitive: each required
service is woken through its own node and guest only. See docs/backup_wake.md.

Pure with respect to infrastructure — DB reads only, no adapters — so it is safe
to call from any synchronous context.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from orchestrator.db.models import BackupPolicy, Node, NodeState, Service
from orchestrator.domain.enums import BackupWakeMode, PowerState

REASON_POLICY = "policy prerequisite"
REASON_SERVICE = "service prerequisite"
REASON_TARGET = "backup target"

#: Shown next to a plan step (and in the pipeline's failure) when the node has no
#: way to be turned on. Kept as one string so the UI, CLI and API agree.
BLOCKER_NO_POWER_TARGET = "node has no power target — cannot be woken"


def _wake_switch_blocker(svc: Service, node: Node) -> str | None:
    """Why a backup that FOLLOWS the wake-on-hit switches may not wake this."""
    if not svc.wake_on_hit_enabled:
        return (
            f"wake on hit is switched off for service {svc.slug!r} and this backup policy "
            "follows it (set the policy's wake mode to 'always' to override)"
        )
    if not node.wake_on_hit_enabled:
        return (
            f"wake on hit is switched off for node {node.name!r} and this backup policy "
            "follows it (set the policy's wake mode to 'always' to override)"
        )
    return None


@dataclass(frozen=True)
class PlanStep:
    """One service the backup run must have awake, in order."""

    slug: str
    name: str
    node: str
    reason: str
    #: "always_on", or the last observed ``PowerState`` value ("unknown" if never seen).
    node_power: str
    #: Why this can never be woken, or None. Only matters if a wake is actually
    #: needed — a blocked node that is up right now is simply up.
    blocker: str | None = None
    node_always_on: bool = False

    @property
    def is_target(self) -> bool:
        return self.reason == REASON_TARGET

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "node": self.node,
            "reason": self.reason,
            "node_power": self.node_power,
            "blocker": self.blocker,
        }


@dataclass
class BackupPlan:
    slug: str
    policy: str | None
    steps: list[PlanStep] = field(default_factory=list)
    #: The policy's wake mode ("follow" / "always" / "never").
    wake_mode: str = BackupWakeMode.FOLLOW.value
    #: Sub-services this backup also waits for (backup_gate: required): each must be
    #: HEALTHY first, and is started through its controller if it is stopped.
    #: ``[{"slug", "name", "controller", "can_start"}]``.
    required_subs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blockers(self) -> list[str]:
        return [f"{s.slug}: {s.blocker}" for s in self.steps if s.blocker]

    @property
    def will_wake(self) -> bool:
        """True when at least one step sits on a node that can be powered down —
        i.e. a run might have to wake something. A plan made only of always-on
        nodes never does."""
        return any(not s.node_always_on for s in self.steps)

    @property
    def prerequisites(self) -> list[PlanStep]:
        return [s for s in self.steps if not s.is_target]

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "policy": self.policy,
            "steps": [s.as_dict() for s in self.steps],
            "blockers": self.blockers,
            "will_wake": self.will_wake,
            "wake_mode": self.wake_mode,
            "required_subs": self.required_subs,
        }


def _node_power(session: Session, node: Node | None) -> str:
    if node is None:
        return PowerState.UNKNOWN.value
    if node.always_on:
        return "always_on"
    state = session.get(NodeState, node.id) if node.id is not None else None
    return state.power_state.value if state is not None else PowerState.UNKNOWN.value


def _step_for(
    session: Session,
    slug: str,
    reason: str,
    mode: BackupWakeMode = BackupWakeMode.FOLLOW,
) -> PlanStep:
    svc = session.exec(select(Service).where(Service.slug == slug)).one_or_none()
    if svc is None:
        return PlanStep(
            slug=slug,
            name=slug,
            node="",
            reason=reason,
            node_power=PowerState.UNKNOWN.value,
            blocker="no such service in the registry",
        )
    node = session.get(Node, svc.node_id) if svc.node_id else None
    blocker: str | None = None
    if not svc.enabled:
        blocker = "service is disabled in the registry"
    elif node is None:
        blocker = "service is not attached to a node"
    else:
        # Same predicate WakePipeline raises WakeUnsupportedError on (blank
        # power target and not always_on) — imported lazily because the wake
        # pipeline module pulls in adapters this module has no business loading.
        from orchestrator.pipelines.wake import wake_is_unsupported

        if wake_is_unsupported(node):
            blocker = BLOCKER_NO_POWER_TARGET
        elif mode is BackupWakeMode.FOLLOW:
            blocker = _wake_switch_blocker(svc, node)
    return PlanStep(
        slug=slug,
        name=svc.name,
        node=node.name if node else "",
        reason=reason,
        node_power=_node_power(session, node),
        blocker=blocker,
        node_always_on=bool(node and node.always_on),
    )


def resolve_backup_plan(
    session: Session, service: Service, policy: BackupPolicy | None
) -> BackupPlan:
    """Ordered wake targets for backing up ``service`` under ``policy``.

    ``policy.requires``, then ``service.backup_requires``, then (when the policy
    has ``wake_before_backup``) the service itself. De-duplicated keeping the
    first position, so a service named twice is woken once, at its earliest
    slot, with the reason of that slot.

    With no policy at all the target is still woken: the default for
    ``wake_before_backup`` is on, and a policy-less manual backup should not
    behave differently from a policy-covered one.
    """
    mode = policy.wake_mode if policy is not None else BackupWakeMode.FOLLOW
    wanted: list[tuple[str, str]] = []
    if policy is not None:
        wanted += [(s, REASON_POLICY) for s in (policy.requires or [])]
    wanted += [(s, REASON_SERVICE) for s in (service.backup_requires or [])]
    if policy is None or policy.wake_before_backup:
        wanted.append((service.slug, REASON_TARGET))

    seen: set[str] = set()
    steps: list[PlanStep] = []
    for slug, reason in wanted:
        if slug in seen:
            continue
        seen.add(slug)
        steps.append(_step_for(session, slug, reason, mode))
    if mode is BackupWakeMode.NEVER:
        steps = []  # this policy wakes nothing; the run uses whatever is up
    from orchestrator.domain.enums import BackupGate
    from orchestrator.registry.tree import TreeIndex

    tree = TreeIndex.build(session)
    subs = [
        {
            "slug": d.slug,
            "name": d.name,
            "controller": str((d.controller or {}).get("type", "")),
            "can_start": bool(d.controller),
        }
        for d in tree.descendants(service)
        if d.backup_gate is BackupGate.REQUIRED and tree.is_served(d)
    ]
    return BackupPlan(
        slug=service.slug,
        policy=policy.name if policy else None,
        steps=steps,
        required_subs=subs,
        wake_mode=mode.value,
    )


def resolve_policy_prerequisites(session: Session, policy: BackupPolicy) -> list[PlanStep]:
    """Just the policy-level prerequisites — what a scheduled run wakes ONCE for
    the whole batch of services the policy backs up."""
    seen: set[str] = set()
    steps: list[PlanStep] = []
    for slug in policy.requires or []:
        if slug not in seen:
            seen.add(slug)
            steps.append(_step_for(session, slug, REASON_POLICY, policy.wake_mode))
    return [] if policy.wake_mode is BackupWakeMode.NEVER else steps


# ---------------------------------------------------------------------------
# validation (shared by the YAML loader, the HTTP API and the CLI editor)
# ---------------------------------------------------------------------------


def clean_slug_list(raw: Iterable[Any] | None) -> list[str]:
    """Strip, drop blanks, collapse duplicates keeping first position."""
    out: list[str] = []
    for item in raw or []:
        slug = str(item).strip()
        if slug and slug not in out:
            out.append(slug)
    return out


def requires_problems(
    *,
    service_requires: dict[str, list[str]],
    service_policy: dict[str, str | None],
    policy_requires: dict[str, list[str]],
    excluded: set[str],
) -> list[str]:
    """Everything wrong with the ``requires`` / ``backup_requires`` graph.

    * a slug that names no service,
    * a service listing itself in ``backup_requires``,
    * a cycle of two or more services.

    The graph edge for a service is its own ``backup_requires`` plus its
    policy's ``requires``. Two deliberate leniencies, both because they are
    harmless rather than because they are right:

    * a policy requiring a service the policy also backs up (a self-loop that
      only exists through the policy) is collapsed by de-duplication at plan time
      and is ignored here;
    * ``backup_excluded`` services are never backed up, so the edges they would
      get from their policy are not followed.

    The cycle finder is imported lazily from the loader to avoid an import cycle
    (the loader imports this module for the same checks).
    """
    from orchestrator.registry.loader import _find_dependency_cycle

    slugs = set(service_requires)
    problems: list[str] = []

    for slug, reqs in service_requires.items():
        for req in reqs:
            if req == slug:
                problems.append(f"service {slug!r} lists itself in backup_requires")
            elif req not in slugs:
                problems.append(
                    f"service {slug!r} backup_requires {req!r}, which is not a known service"
                )
    for name, reqs in policy_requires.items():
        for req in reqs:
            if req not in slugs:
                problems.append(
                    f"backup policy {name!r} requires {req!r}, which is not a known service"
                )

    graph: dict[str, list[str]] = {}
    for slug, reqs in service_requires.items():
        edges = [r for r in reqs if r != slug]
        policy = service_policy.get(slug)
        if policy and slug not in excluded:
            edges += [r for r in policy_requires.get(policy, []) if r != slug]
        graph[slug] = edges
    cycle = _find_dependency_cycle(graph)
    if cycle:
        problems.append("backup requirements form a cycle: " + " -> ".join(cycle))
    return problems


def requires_problems_from_db(
    session: Session,
    *,
    service_requires_override: dict[str, list[str]] | None = None,
    policy_requires_override: dict[str, list[str]] | None = None,
    service_policy_override: dict[str, str | None] | None = None,
) -> list[str]:
    """:func:`requires_problems` over the live DB, with optional hypothetical
    edits applied — what an API/CLI write checks BEFORE committing it."""
    services = session.exec(select(Service)).all()
    policies = session.exec(select(BackupPolicy)).all()
    policy_name = {p.id: p.name for p in policies}

    service_requires = {s.slug: list(s.backup_requires or []) for s in services}
    service_policy = {s.slug: policy_name.get(s.backup_policy_id) for s in services}
    policy_requires = {p.name: list(p.requires or []) for p in policies}
    service_requires.update(service_requires_override or {})
    service_policy.update(service_policy_override or {})
    policy_requires.update(policy_requires_override or {})
    return requires_problems(
        service_requires=service_requires,
        service_policy=service_policy,
        policy_requires=policy_requires,
        excluded={s.slug for s in services if s.backup_excluded},
    )
