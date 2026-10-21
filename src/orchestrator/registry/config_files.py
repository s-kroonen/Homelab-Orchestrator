"""Which Traefik Manager config file a proxy host's route lives in.

Traefik Manager (TM) writes every route into one of its dynamic-config files.
Its ``POST /save`` takes an optional ``configFile``; what it then does is easy to
get wrong:

* a name in TM's ``CONFIG_PATHS`` (or already in its ``CONFIG_DIR``) -> that file;
* a bare name while TM runs with ``CONFIG_DIR`` -> TM creates ``<name>.yml`` there;
* anything else (a fixed ``CONFIG_PATHS`` list, a name with a slash) -> TM
  quietly falls back to its DEFAULT file, i.e. the route lands somewhere the
  operator did not ask for.

So the orchestrator resolves the file itself (this module: pure functions, no
I/O) and refuses a name TM would silently replace (:func:`unknown_file_problem`)
instead of posting it. The operator's convention is one file per service,
``<service-slug>.yml``, with ``_shared.yml`` kept for shared middlewares - hence
the default template ``{service}.yml`` (``TRAEFIK_MANAGER_CONFIG_FILE``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from orchestrator.adapters.proxy.base import ConfigFiles

#: Placeholders a ``TRAEFIK_MANAGER_CONFIG_FILE`` template may use.
PLACEHOLDERS = ("service", "hostname")

DEFAULT_TEMPLATE = "{service}.yml"

_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def normalize_config_file(value: str | None) -> str:
    """A bare config file name with a ``.yml``/``.yaml`` extension; ``""`` stays
    ``""`` (= use the template). Raises ``ValueError`` for anything that is not a
    plain file name (a path, spaces, ``..``) - Traefik Manager refuses those and
    falls back to its default file."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if "/" in raw or "\\" in raw:
        raise ValueError(
            f"{raw!r} must be a plain file name (no directory) - Traefik Manager only writes "
            "to files in its config directory."
        )
    if ".." in raw or not _NAME_RE.match(raw):
        raise ValueError(
            f"{raw!r} is not a valid config file name (letters, digits, '.', '_' and '-' only)."
        )
    if not raw.lower().endswith((".yml", ".yaml")):
        raw += ".yml"
    return raw


def check_template(template: str) -> str:
    """Validate a ``TRAEFIK_MANAGER_CONFIG_FILE`` template and return it
    stripped. Raises ``ValueError`` on an unknown placeholder or a template that
    cannot produce a plain file name."""
    text = (template or "").strip()
    if not text:
        return ""
    try:
        text.format(service="svc", hostname="host-example-com")
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            f"TRAEFIK_MANAGER_CONFIG_FILE {template!r} is not a valid template "
            f"(placeholders: {', '.join('{' + p + '}' for p in PLACEHOLDERS)}): {exc}"
        ) from exc
    normalize_config_file(text.format(service="svc", hostname="host-example-com"))
    return text


def resolve_config_file(
    template: str,
    *,
    service_slug: str | None,
    hostname: str,
    override: str | None = None,
) -> str:
    """The file a NEW route for this host goes into: the per-host ``override``
    when set, else ``template`` with ``{service}`` = the linked service slug and
    ``{hostname}`` = the hostname with dots turned into dashes. ``""`` = Traefik
    Manager's default file (blank template)."""
    if (override or "").strip():
        return normalize_config_file(override)
    text = (template or "").strip()
    if not text:
        return ""
    name = text.format(
        service=(service_slug or "").strip(),
        hostname=hostname.strip().lower().replace(".", "-"),
    )
    return normalize_config_file(name)


def same_file(a: str, b: str) -> bool:
    """Config files are compared by base name (Traefik Manager reports bare names;
    names are case sensitive)."""
    return a.strip().rsplit("/", 1)[-1] == b.strip().rsplit("/", 1)[-1]


def known_names(known: ConfigFiles | None, extra: Iterable[str] = ()) -> list[str]:
    """Known file names plus ``extra`` (e.g. the files routes were seen in),
    de-duplicated and sorted."""
    names = {n for n in (known.files if known is not None else ()) if n}
    names.update(n for n in extra if n)
    return sorted(names)


def unknown_file_problem(wanted: str, known: ConfigFiles | None) -> str | None:
    """A message when Traefik Manager would NOT put a route into ``wanted`` (and
    would use its default file instead), else ``None``.

    ``known`` is what TM reported (``None`` = unreadable: nothing to check
    against, so nothing is refused). A name TM can create (``CONFIG_DIR`` mode)
    or already has is fine.
    """
    if not wanted or known is None:
        return None
    if any(same_file(wanted, f) for f in known.files):
        return None
    if known.can_create is not False:
        return None
    available = ", ".join(known.files) or "(none)"
    return (
        f'Traefik Manager has no config file "{wanted}" and cannot create it: it can only write '
        f"to the files in its CONFIG_PATHS ({available}). Add {wanted} to CONFIG_PATHS "
        "(or switch Traefik Manager to CONFIG_DIR, which creates files on demand), or pick one "
        "of the existing files."
    )


def effective_file(
    wanted: str, known: ConfigFiles | None, *, explicit: bool
) -> tuple[str, str | None]:
    """``(file to post, problem)`` for a resolved name.

    A template-derived name on a Traefik Manager with a single fixed config file
    is dropped (``""`` = that one file): there is nowhere else to put the route,
    and failing every push because ``{service}.yml`` does not exist would make
    the default template unusable there. An explicit per-host name is always
    checked.
    """
    if not wanted:
        return "", None
    if not explicit and known is not None and known.single:
        return "", None
    return wanted, unknown_file_problem(wanted, known)
