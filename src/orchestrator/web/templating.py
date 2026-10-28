"""The one Jinja environment every page renders with.

Pages used to build their own ``Jinja2Templates`` each; sharing one gives every template the same
globals:

* ``asset("app.css")`` -> ``/static/app.css?v=<hash of the file>``. The hash changes when the file
  does, so :class:`~orchestrator.web.static_files.RevalidatingStaticFiles` can tell browsers to
  keep a versioned URL for a year without ever serving a stale file after an upgrade.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"

#: path -> (mtime_ns, size, short hash)
_hashes: dict[str, tuple[int, int, str]] = {}


def asset_version(path: str) -> str:
    """A short content hash of ``static/<path>``, recomputed only when the file changes."""
    file = STATIC_DIR / path
    try:
        st = file.stat()
    except OSError:
        return "0"
    known = _hashes.get(path)
    if known and known[0] == st.st_mtime_ns and known[1] == st.st_size:
        return known[2]
    digest = hashlib.sha256(file.read_bytes()).hexdigest()[:12]
    _hashes[path] = (st.st_mtime_ns, st.st_size, digest)
    return digest


def asset(path: str) -> str:
    """The URL of a static file, versioned by its content."""
    return f"/static/{path}?v={asset_version(path)}"


templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["asset"] = asset
