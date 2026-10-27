"""Between ``.env``-style values and connections.

The setup wizard and ``config edit`` think in ``KEY=value`` pairs (one set per install). The
adapter-related pairs no longer belong in ``.env``: they are connections. These functions turn
the pairs into connections when the wizard saves, and connections back into pairs when it loads,
so the questions it asks stay the same while the answers land in ``connections.yaml``.
"""

from __future__ import annotations

from pydantic import SecretStr

from orchestrator.connections.kinds import KINDS, MANAGED_SETTINGS
from orchestrator.connections.model import Connection, ConnectionError, coerce
from orchestrator.connections.store import primary

#: ``.env`` keys that live in connections.yaml now.
MANAGED_ENV: frozenset[str] = frozenset(name.upper() for name in MANAGED_SETTINGS)


def split_values(values: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """(what stays in .env, what becomes connections)."""
    keep = {k: v for k, v in values.items() if k not in MANAGED_ENV}
    moved = {k: v for k, v in values.items() if k in MANAGED_ENV}
    return keep, moved


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def from_values(values: dict[str, str], ids: dict[str, str] | None = None) -> list[Connection]:
    """The connections the managed pairs in ``values`` describe: one per kind whose marker field
    is filled in (a placeholder such as ``proxmox.example.lan`` counts as blank). Fields left at
    their default are not written, so the file stays short. ``ids`` names the connection each
    kind's answers belong to (default: the kind's own name)."""
    out: list[Connection] = []
    for spec in KINDS:
        marker = spec.field(spec.marker)
        raw = _text(values.get(marker.setting.upper())).strip()
        filled = bool(raw) and raw not in (str(marker.setting_default), str(marker.default))
        plain: dict[str, object] = {}
        secret: dict[str, str] = {}
        others = False  # something besides the marker was typed
        for f in spec.fields:
            text = _text(values.get(f.setting.upper()))
            if f.is_secret:
                if text:
                    secret[f.name] = text
                    others = True
                continue
            if f.name == spec.marker:
                if filled:
                    plain[f.name] = coerce(f, text)
                continue
            if text.strip() == "" or text == str(f.setting_default):
                continue
            value = coerce(f, text)
            if value != f.default:
                plain[f.name] = value
                others = True
        if not filled and not others:
            continue
        conn = Connection(
            id=(ids or {}).get(spec.kind, spec.kind),
            kind=spec.kind,
            name=spec.title,
            enabled=filled,
            default=True,
            settings=plain,
            secrets=secret,
        )
        try:
            conn.validate_values()
        except ConnectionError:
            conn.enabled = False  # half filled in: keep what was typed, switched off
            conn.validate_values()
        out.append(conn)
    return out


def to_values(conns: list[Connection]) -> dict[str, str]:
    """The ``KEY=value`` pairs for the connection of each kind that is in use."""
    out: dict[str, str] = {}
    for spec in KINDS:
        # a switched-off connection still shows what was typed, so editing it loses nothing
        conn = primary(conns, spec.kind) or next((c for c in conns if c.kind == spec.kind), None)
        if conn is None:
            continue
        for f in spec.fields:
            out[f.setting.upper()] = _text(conn.value(f))
    return out
