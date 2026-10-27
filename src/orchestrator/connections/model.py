"""One connection: what it is, whether it is on, and its values.

Values are kept in two maps so the file reads clearly and secrets are easy to mask: ``settings``
(plain) and ``secrets`` (never shown again once saved). Both are keyed by the kind's field names
(see :mod:`kinds`).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.connections.kinds import FieldSpec, KindSpec, kind_spec

SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
Scope = Literal["local", "shared"]


class ConnectionError(ValueError):
    """The connection (or the set of them) is not valid; the message says why."""


def coerce(field: FieldSpec, raw: Any) -> Any:
    """``raw`` as the type the field has (a form or a YAML file may hand over strings)."""
    kind = field.type
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off", ""}:
            return False
        raise ConnectionError(f"{field.label}: {raw!r} is not yes or no")
    if kind in {"int", "float"}:
        if raw is None or str(raw).strip() == "":
            return field.default
        try:
            return int(raw) if kind == "int" else float(raw)
        except (TypeError, ValueError):
            raise ConnectionError(f"{field.label}: {raw!r} is not a number") from None
    return "" if raw is None else str(raw).strip()


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    name: str = ""
    enabled: bool = True
    #: Among several of one kind, the one used when nothing names a connection.
    default: bool = False
    #: Where it lives ("home", "offsite", ...). Free text; the second orchestrator uses it to
    #: tell its own connections from the other site's.
    site: str = ""
    #: ``shared``: the same connection on every orchestrator (exported with the shared set);
    #: ``local``: only reachable from this one (its own LAN's Proxmox, say).
    scope: Scope = "local"
    notes: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    #: Not stored: "file", or "env" for one worked out from ``.env`` (not moved yet).
    source: str = Field(default="file", exclude=True)

    @property
    def spec(self) -> KindSpec:
        return kind_spec(self.kind)

    @property
    def label(self) -> str:
        return self.name or self.spec.title

    def value(self, field: FieldSpec) -> Any:
        """The stored value, coerced, or the Settings default."""
        bag = self.secrets if field.is_secret else self.settings
        if field.name in bag and not (bag[field.name] in (None, "") and field.type != "text"):
            return coerce(field, bag[field.name])
        return field.default

    def has_secret(self, field: FieldSpec) -> bool:
        return bool(self.secrets.get(field.name))

    @property
    def configured(self) -> bool:
        return bool(str(self.value(self.spec.field(self.spec.marker)) or "").strip())

    def validate_values(self) -> None:
        """Raise :class:`ConnectionError` for anything wrong; normalises the maps in place."""
        if not SLUG.match(self.id):
            raise ConnectionError(
                f"id {self.id!r}: use lowercase letters, digits, '-' or '_' (max 40, starting "
                "with a letter or digit)"
            )
        spec = kind_spec(self.kind)
        plain = {f.name: f for f in spec.fields if not f.is_secret}
        secret = {f.name: f for f in spec.fields if f.is_secret}
        for key in self.settings:
            if key not in plain:
                where = "belongs in secrets" if key in secret else "is not a field of"
                raise ConnectionError(f"{self.id}: {key!r} {where} {spec.kind}")
        for key in self.secrets:
            if key not in secret:
                raise ConnectionError(f"{self.id}: {key!r} is not a secret field of {spec.kind}")
        self.settings = {k: coerce(plain[k], v) for k, v in self.settings.items()}
        self.secrets = {k: str(v) for k, v in self.secrets.items() if str(v) != ""}
        if self.enabled:
            for f in spec.fields:
                if f.required and not str(self.value(f)).strip():
                    raise ConnectionError(f"{self.id}: {f.label} is required")
        self.site = self.site.strip()
        self.name = self.name.strip()

    def public(self) -> dict[str, Any]:
        """For the API: secrets are only ever reported as set / not set."""
        spec = self.spec
        return {
            "id": self.id,
            "kind": self.kind,
            "kind_title": spec.title,
            "name": self.label,
            "enabled": self.enabled,
            "default": self.default,
            "site": self.site,
            "scope": self.scope,
            "notes": self.notes,
            "source": self.source,
            "configured": self.configured,
            "settings": {f.name: self.value(f) for f in spec.fields if not f.is_secret},
            "secrets_set": {f.name: self.has_secret(f) for f in spec.fields if f.is_secret},
        }
