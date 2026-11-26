"""How long a guest's dump and verify may take: learned from its own recent runs (OR-98).

A fixed deadline is wrong both ways: a small container verifies in a minute, so a stuck task
waits half an hour before anyone hears of it, and a big VM on slow storage can need more than
the default and is called failed while PBS is still working. So each deadline comes from the
guest's own history:

* the durations of the last :data:`HISTORY` dumps (``vzdump_finished``) or verifies
  (``pbs_verify``) that worked, read from its backup runs' steps;
* deadline = ``backup_deadline_factor`` x the slowest of them + :data:`MARGIN_S`;
* never under ``backup_deadline_min_s``, never over ``backup_task_max_s`` /
  ``verify_task_max_s``.

With no history yet it is the setting (``BACKUP_TASK_TIMEOUT_S`` / ``VERIFY_TASK_TIMEOUT_S``),
and for a verify, at least the snapshot's size read at ``VERIFY_MIN_MIB_S``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlmodel import Session, col, select

from orchestrator.config import Settings
from orchestrator.db.models import PipelineRun
from orchestrator.domain.enums import PipelineKind

HISTORY = 5
MARGIN_S = 300
#: How many of the guest's newest backup runs are read for that history.
_RUNS_READ = 30
_STEP = {"dump": "vzdump_finished", "verify": "pbs_verify"}


@dataclass(frozen=True)
class Deadline:
    seconds: int
    why: str
    history_s: tuple[int, ...] = ()

    def as_detail(self) -> dict[str, Any]:
        return {
            "deadline_s": self.seconds,
            "deadline_why": self.why,
            "history_s": list(self.history_s),
        }


def _parse(at: Any) -> datetime | None:
    if not at:
        return None
    try:
        return datetime.fromisoformat(str(at))
    except ValueError:
        return None


def step_seconds(step: dict[str, Any]) -> int | None:
    """How long one recorded step took, from its own times."""
    start, end = _parse(step.get("started_at")), _parse(step.get("finished_at"))
    if start is None or end is None:
        return None
    if (start.tzinfo is None) != (end.tzinfo is None):
        start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
    seconds = int((end - start).total_seconds())
    return seconds if seconds >= 0 else None


def history(
    session: Session, service_id: int | None, kind: str, *, limit: int = HISTORY
) -> list[int]:
    """Durations in seconds of the guest's last ``limit`` dumps or verifies that worked,
    newest first."""
    if service_id is None:
        return []
    name = _STEP[kind]
    runs = session.exec(
        select(PipelineRun)
        .where(PipelineRun.kind == PipelineKind.BACKUP, PipelineRun.service_id == service_id)
        .order_by(col(PipelineRun.started_at).desc())
        .limit(_RUNS_READ)
    ).all()
    out: list[int] = []
    for run in runs:
        for step in run.steps or []:
            if step.get("name") != name or step.get("status") != "ok":
                continue
            seconds = step_seconds(step)
            if seconds is not None:
                out.append(seconds)
            break
        if len(out) >= limit:
            break
    return out


def _human(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    hours, rest = divmod(seconds, 3600)
    return f"{hours} h {round(rest / 60)} min" if rest >= 60 else f"{hours} h"


def deadline(
    session: Session,
    settings: Settings,
    service_id: int | None,
    kind: str,
    *,
    size_bytes: int | None = None,
) -> Deadline:
    """The deadline for the guest's next dump (``kind="dump"``) or verify (``"verify"``)."""
    floor = max(1, int(settings.backup_deadline_min_s))
    if kind == "dump":
        default, ceiling = settings.backup_task_timeout_s, settings.backup_task_max_s
    else:
        default, ceiling = settings.verify_task_timeout_s, settings.verify_task_max_s
    ceiling = max(floor, int(ceiling))
    past = history(session, service_id, kind)
    if past:
        slowest = max(past)
        raw = slowest * float(settings.backup_deadline_factor) + MARGIN_S
        why = (
            f"{settings.backup_deadline_factor:g}x the slowest of its last {len(past)} "
            f"({_human(slowest)}) + {_human(MARGIN_S)}"
        )
    else:
        raw = float(default)
        why = f"no history yet: the default {_human(default)}"
        if kind == "verify" and size_bytes and settings.verify_min_mib_s > 0:
            by_size = size_bytes / (settings.verify_min_mib_s * 1024 * 1024)
            if by_size > raw:
                raw = by_size
                why = (
                    f"no history yet: {size_bytes / 1024**3:.0f} GiB read at "
                    f"{settings.verify_min_mib_s:g} MiB/s"
                )
    seconds = int(min(ceiling, max(floor, raw)))
    if seconds == ceiling and raw > ceiling:
        why += f", capped at {_human(ceiling)}"
    elif seconds == floor and raw < floor:
        why += f", raised to the minimum {_human(floor)}"
    return Deadline(seconds=seconds, why=why, history_s=tuple(past))


def hold_ttl_s(session: Session, settings: Settings, service_id: int | None) -> int:
    """How long a hold must last for one more backup of this guest: its dump's and its
    verify's deadline, plus the wake."""
    dump = deadline(session, settings, service_id, "dump").seconds
    verify = deadline(session, settings, service_id, "verify").seconds
    return settings.wake_timeout_s * 2 + dump + verify


__all__ = ["Deadline", "deadline", "history", "hold_ttl_s", "step_seconds"]
