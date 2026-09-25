"""Surgical editor for ``services.yaml``.

Why this exists separately from :mod:`orchestrator.registry.loader`: the loader
reconciles YAML *into the DB*. This edits the YAML *file itself*, which is what
the CLI config builders need — the DB is rehydrated from the file on every boot,
so writing config to the DB would silently vanish on restart.

Two properties this deliberately preserves:

* **Comments and formatting survive.** Edits mutate the ruamel round-trip
  document in place rather than dumping a Pydantic model over it. An operator
  who annotated their config does not lose those notes because they added a
  probe from the CLI.
* **The file is validated before it is written.** Every mutation is checked
  against :class:`RegistryFile`, so a bad edit fails with a message instead of
  leaving an unloadable registry on disk. Combined with the atomic tmp+rename
  write, a failed edit leaves the previous file intact.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from orchestrator.registry.schema import RegistryFile

_yaml = YAML(typ="rt")
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.preserve_quotes = True
_yaml.width = 4096  # don't hard-wrap long URLs into unreadable continuations
# Keep the document-start marker: yamllint's default config warns without it,
# and dropping it on every CLI edit would reintroduce that warning.
_yaml.explicit_start = True


class RegistryEditError(Exception):
    """An edit could not be applied, or would have produced an invalid file."""


class RegistryEditor:
    """Load, mutate, validate and save ``services.yaml``."""

    def __init__(self, path: Path, *, fresh: bool = False) -> None:
        """``fresh=True`` starts from an empty registry and ignores the file on disk.

        That is what ``init`` needs: it replaces the file wholesale, and merging into
        the old one would quietly keep services the operator just chose to leave out.
        """
        self.path = path
        self.doc: CommentedMap = self._empty() if fresh else self._load()

    @staticmethod
    def _empty() -> CommentedMap:
        # The top-level keys must exist, otherwise every later mutation has to
        # special-case "does this key exist".
        doc = CommentedMap()
        doc["version"] = 1
        doc["nodes"] = CommentedSeq()
        doc["backup_policies"] = CommentedSeq()
        doc["services"] = CommentedSeq()
        return doc

    # -- loading / saving ---------------------------------------------------

    def _load(self) -> CommentedMap:
        if not self.path.exists():
            return self._empty()

        if self.path.is_dir():
            raise RegistryEditError(
                f"{self.path} is a directory, not a file (usually a Docker bind "
                f"mount whose host file did not exist)"
            )

        loaded = _yaml.load(io.BytesIO(self.path.read_bytes()))
        if loaded is None:
            loaded = CommentedMap()
        for key, default in (
            ("version", 1),
            ("nodes", CommentedSeq()),
            ("backup_policies", CommentedSeq()),
            ("services", CommentedSeq()),
        ):
            if key not in loaded:
                loaded[key] = default
        return loaded

    def validate(self) -> RegistryFile:
        """Check the in-memory document is one the app can actually load.

        Two layers, and both matter: the schema catches shape errors, and the
        reference check catches a service pointing at a node or policy the file
        does not define. Without the second, the CLI could happily write a file
        that the orchestrator then refuses at boot — the worst place to find out.
        """
        # Imported here rather than at module scope: loader imports nothing from
        # this module, and keeping it that way avoids a cycle.
        from orchestrator.registry.loader import validate_references

        try:
            parsed = RegistryFile.model_validate(_plain(self.doc))
        except Exception as exc:
            raise RegistryEditError(
                f"the edit would produce an invalid services.yaml: {exc}"
            ) from exc

        try:
            validate_references(parsed)
        except ValueError as exc:
            raise RegistryEditError(
                f"the edit would produce an invalid services.yaml: {exc}"
            ) from exc
        return parsed

    def save(self) -> str:
        """Validate then write atomically. Returns the sha256 of what was written."""
        self.validate()
        buf = io.BytesIO()
        _yaml.dump(self.doc, buf)
        data = buf.getvalue()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(self.path)
        return hashlib.sha256(data).hexdigest()

    # -- nodes / policies ---------------------------------------------------

    def node_names(self) -> list[str]:
        return [str(n.get("name")) for n in self.doc.get("nodes", [])]

    def ensure_node(
        self,
        name: str,
        *,
        always_on: bool | None = None,
        power_mgr_target: str | None = None,
        notes: str | None = None,
    ) -> bool:
        """Add the node if absent; update the given fields if present.

        Returns True if anything changed.
        """
        for node in self.doc.setdefault("nodes", CommentedSeq()):
            if node.get("name") == name:
                changed = False
                for key, value in (
                    ("always_on", always_on),
                    ("power_mgr_target", power_mgr_target),
                    ("notes", notes),
                ):
                    if value is not None and node.get(key) != value:
                        node[key] = value
                        changed = True
                return changed

        entry = CommentedMap()
        entry["name"] = name
        entry["always_on"] = bool(always_on)
        entry["power_mgr_target"] = power_mgr_target if power_mgr_target is not None else name
        entry["notes"] = notes or ""
        self.doc["nodes"].append(entry)
        return True

    def policy_names(self) -> list[str]:
        return [str(p.get("name")) for p in self.doc.get("backup_policies", [])]

    def ensure_policy(self, name: str, **fields: Any) -> bool:
        for policy in self.doc.setdefault("backup_policies", CommentedSeq()):
            if policy.get("name") == name:
                return False
        entry = CommentedMap()
        entry["name"] = name
        entry["schedule_cron"] = fields.get("schedule_cron", "")
        entry["mode"] = fields.get("mode", "snapshot")
        entry["retention"] = fields.get("retention", {"keep_daily": 7})
        entry["targets"] = fields.get("targets", {})
        self.doc["backup_policies"].append(entry)
        return True

    # -- services -----------------------------------------------------------

    def service_slugs(self) -> list[str]:
        return [str(s.get("slug")) for s in self.doc.get("services", [])]

    def get_service(self, slug: str) -> CommentedMap | None:
        for svc in self.doc.get("services", []):
            if svc.get("slug") == slug:
                return svc
        return None

    def require_service(self, slug: str) -> CommentedMap:
        svc = self.get_service(slug)
        if svc is None:
            known = ", ".join(sorted(self.service_slugs())) or "(none)"
            raise RegistryEditError(f"no service {slug!r} in {self.path}. Known: {known}")
        return svc

    def upsert_service(self, slug: str, fields: dict[str, Any]) -> str:
        """Create or update a service. Returns "added" or "updated".

        Only the keys present in ``fields`` are touched, so updating one attribute
        never silently resets the others — important when the CLI is editing a
        config a human has already tuned.
        """
        existing = self.get_service(slug)
        if existing is not None:
            for key, value in fields.items():
                existing[key] = value
            return "updated"

        entry = CommentedMap()
        entry["slug"] = slug
        # Fixed key order so generated entries read consistently.
        for key in (
            "name",
            "description",
            "node",
            "guest_kind",
            "guest_id",
            "enabled",
            "backup_excluded",
            "backup_excluded_reason",
            "backup_policy",
        ):
            if key in fields:
                entry[key] = fields[key]
        entry.setdefault("probes", CommentedSeq())
        entry.setdefault("proxy_hosts", CommentedSeq())
        for key, value in fields.items():
            if key not in entry:
                entry[key] = value
        self.doc.setdefault("services", CommentedSeq()).append(entry)
        return "added"

    def remove_service(self, slug: str) -> bool:
        services = self.doc.get("services", CommentedSeq())
        for i, svc in enumerate(services):
            if svc.get("slug") == slug:
                _seq_delete(self.doc, "services", i)
                return True
        return False

    # -- probes -------------------------------------------------------------

    def probes(self, slug: str) -> CommentedSeq:
        svc = self.require_service(slug)
        if "probes" not in svc or svc["probes"] is None:
            svc["probes"] = CommentedSeq()
        return svc["probes"]

    def upsert_probe(self, slug: str, probe: dict[str, Any]) -> str:
        """Add a probe, or replace the one with the same name."""
        name = probe.get("name")
        if not name:
            raise RegistryEditError("a probe needs a name")
        probes = self.probes(slug)
        for i, existing in enumerate(probes):
            if existing.get("name") == name:
                probes[i] = _to_commented(probe)
                return "updated"
        probes.append(_to_commented(probe))
        return "added"

    def remove_probe(self, slug: str, name: str) -> bool:
        svc = self.require_service(slug)
        probes = self.probes(slug)
        for i, probe in enumerate(probes):
            if probe.get("name") == name:
                _seq_delete(svc, "probes", i)
                return True
        return False

    # -- proxy hosts --------------------------------------------------------

    def proxy_hosts(self, slug: str) -> CommentedSeq:
        svc = self.require_service(slug)
        if "proxy_hosts" not in svc or svc["proxy_hosts"] is None:
            svc["proxy_hosts"] = CommentedSeq()
        return svc["proxy_hosts"]

    def upsert_proxy_host(self, slug: str, host: dict[str, Any]) -> str:
        hostname = host.get("hostname")
        if not hostname:
            raise RegistryEditError("a proxy host needs a hostname")
        hosts = self.proxy_hosts(slug)
        for i, existing in enumerate(hosts):
            if existing.get("hostname") == hostname:
                hosts[i] = _to_commented(host)
                return "updated"
        hosts.append(_to_commented(host))
        return "added"

    def remove_proxy_host(self, slug: str, hostname: str) -> bool:
        svc = self.require_service(slug)
        hosts = self.proxy_hosts(slug)
        for i, host in enumerate(hosts):
            if host.get("hostname") == hostname:
                _seq_delete(svc, "proxy_hosts", i)
                return True
        return False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seq_delete(parent: Any, key: str, index: int) -> None:
    """Delete ``parent[key][index]``, taking any orphaned comment with it.

    ruamel attaches a comment that precedes the first list item to the PARENT
    mapping's entry for that key, not to the list. Deleting the last item then
    emits the comment above a column-0 ``[]``:

        services:
          # a comment about the service you just removed
        []

    which is not valid YAML, so the file fails to reload. Clear both the
    sequence's own pre-comment and the parent's entry once the list is empty.
    Per-item comments are also reindexed, since ruamel keys them by position.
    """
    seq = parent[key]
    items = getattr(seq.ca, "items", None)
    if items:
        items.pop(index, None)
        for pos in sorted(k for k in items if isinstance(k, int) and k > index):
            items[pos - 1] = items.pop(pos)

    del seq[index]

    if len(seq) == 0:
        if getattr(seq.ca, "items", None):
            seq.ca.items.clear()
        seq.ca.comment = None
        parent_ca = getattr(parent, "ca", None)
        if parent_ca is not None and key in parent_ca.items:
            parent_ca.items.pop(key, None)


def _to_commented(value: Any) -> Any:
    """Convert plain dicts/lists into ruamel containers so they round-trip."""
    if isinstance(value, dict):
        out = CommentedMap()
        for k, v in value.items():
            out[k] = _to_commented(v)
        return out
    if isinstance(value, list):
        seq = CommentedSeq()
        for item in value:
            seq.append(_to_commented(item))
        return seq
    return value


def _plain(value: Any) -> Any:
    """Strip ruamel types back to plain Python for Pydantic validation."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value
