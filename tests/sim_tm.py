"""A faithful little Traefik Manager for the multi-file ``file::name`` id cases.

Reproduces the behaviours of the real app (``app.py``) that matter here:

* with several config files (``multi=True``) ``GET /api/routes`` gives every
  route ``id = "<file>::<name>"`` and ``name = "<name>"``; with one file they
  are equal;
* ``POST /api/routes/<id>/toggle``: disabling parks the router in the
  disabled-routes store under the id it was sent; enabling only finds a record
  stored under EXACTLY that id; anything else is a SILENT NO-OP that still
  answers 200;
* ``POST /delete/<id>``: strips the ``<file>::`` prefix and then searches every
  config file for the bare name unless the form names ``configFile``; falls
  back to the disabled store;
* ``POST /save``: writes into ``configFile`` (default ``dynamic.yml``), and an
  edit whose ``originalId`` lives in another file is not found there - it
  creates a second copy in the default file.

``ignore_toggle`` makes toggles do nothing at all (a 200 that lied).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, unquote

import httpx

DEFAULT_FILE = "dynamic.yml"


class SimTM:
    def __init__(self, files: tuple[str, ...] = (DEFAULT_FILE, "apps.yml"), multi: bool = True):
        self.multi = multi
        self.files: dict[str, dict[str, dict[str, Any]]] = {f: {} for f in files}
        self.disabled: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.ignore_toggle = False
        self.toggle_requests: list[httpx.Request] = []

    # -- helpers -------------------------------------------------------------

    def add(
        self,
        file: str,
        name: str,
        hostname: str,
        *,
        eps: tuple[str, ...] = ("websecure",),
        upstream: str = "http://10.0.0.9:8080",
    ) -> str:
        self.files[file][name] = {
            "rule": f"Host(`{hostname}`)",
            "eps": list(eps),
            "upstream": upstream,
        }
        return self.app_id(file, name)

    def app_id(self, file: str, name: str) -> str:
        return f"{file}::{name}" if self.multi else name

    def locate(self, name: str, file: str = "") -> str | None:
        for f, routes in self.files.items():
            if (not file or f == file) and name in routes:
                return f
        return None

    def enabled_ids(self) -> set[str]:
        return {self.app_id(f, n) for f, routes in self.files.items() for n in routes}

    def is_enabled(self, route_id: str) -> bool | None:
        if route_id in self.enabled_ids():
            return True
        if route_id in self.disabled:
            return False
        return None

    def calls(self, kind: str) -> list[Any]:
        return [body for k, _path, body in self.requests if k == kind]

    # -- http ----------------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        method = request.method
        if path == "/api/routes":
            return httpx.Response(200, json=self._routes())
        if path == "/api/traefik/entrypoints":
            return httpx.Response(
                200,
                json=[
                    {"name": "web", "address": ":80"},
                    {"name": "websecure", "address": ":443"},
                    {"name": "traefik", "address": ":8080"},
                ],
            )
        if path == "/api/settings":
            return httpx.Response(200, json={"cert_resolver": "", "domains": ["example.com"]})
        if path == "/api/traefik/middlewares":
            return httpx.Response(200, json={"http": [{"name": "redirect-to-https@file"}]})
        if path == "/api/traefik/routers":
            return httpx.Response(
                200, json={"http": self._runtime(), "tcp": [], "udp": [], "reachable": True}
            )
        if method == "POST" and path.startswith("/api/routes/") and path.endswith("/toggle"):
            route_id = path.removeprefix("/api/routes/").removesuffix("/toggle")
            enable = json.loads(request.read())["enable"]
            self.requests.append(("toggle", route_id, enable))
            self.toggle_requests.append(request)
            if not self.ignore_toggle:
                self._toggle(route_id, bool(enable))
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path.startswith("/delete/"):
            route_id = path.removeprefix("/delete/")
            form = dict(parse_qsl(request.read().decode(), keep_blank_values=True))
            self.requests.append(("delete", route_id, form.get("configFile", "")))
            return self._delete(route_id, form.get("configFile", ""))
        if method == "POST" and path == "/save":
            form = dict(parse_qsl(request.read().decode(), keep_blank_values=True))
            self.requests.append(("save", form["serviceName"], form))
            self._save(form)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": True})

    def _routes(self) -> dict[str, Any]:
        apps: list[dict[str, Any]] = []
        for f, routes in self.files.items():
            for name, r in routes.items():
                apps.append(
                    {
                        "id": self.app_id(f, name),
                        "name": name,
                        "protocol": "http",
                        "rule": r["rule"],
                        "servers": [r["upstream"]],
                        "entryPoints": r["eps"],
                        "enabled": True,
                        "configFile": f if self.multi else "",
                    }
                )
        for key, snap in self.disabled.items():
            apps.append(
                {
                    "id": key,
                    "name": key.split("::", 1)[-1],
                    "protocol": "http",
                    "rule": snap["route"]["rule"],
                    "target": snap["route"]["upstream"],
                    "entryPoints": snap["route"]["eps"],
                    "enabled": False,
                    "configFile": snap["file"] if self.multi else "",
                }
            )
        return {"apps": apps, "middlewares": [], "services": {"http": []}}

    def _runtime(self) -> list[dict[str, Any]]:
        return [
            {
                "name": f"{name}@file",
                "rule": r["rule"],
                "service": f"{name}-service@file",
                "entryPoints": r["eps"],
                "status": "enabled",
            }
            for routes in self.files.values()
            for name, r in routes.items()
        ]

    def _toggle(self, route_id: str, enable: bool) -> None:
        rname = route_id.split("::", 1)[1] if "::" in route_id else route_id
        if enable:
            snap = self.disabled.pop(route_id, None)
            if snap is not None:  # a record under any other key is never found
                self.files[snap["file"]][rname] = snap["route"]
            return
        prefix = route_id.split("::", 1)[0] if "::" in route_id else ""
        file = self.locate(rname, prefix if prefix in self.files else "")
        if file is None:
            return
        self.disabled[route_id] = {"file": file, "route": self.files[file].pop(rname)}

    def _delete(self, route_id: str, config_file: str) -> httpx.Response:
        plain = route_id.split("::", 1)[1] if "::" in route_id else route_id
        file = self.locate(plain, config_file)
        if file is not None:
            del self.files[file][plain]
            return httpx.Response(200, json={"ok": True})
        for key in (route_id, plain):
            if key in self.disabled:
                del self.disabled[key]
                return httpx.Response(200, json={"ok": True})
        for key in list(self.disabled):
            if key.split("::", 1)[-1] == plain:
                del self.disabled[key]
                return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    def _save(self, form: dict[str, str]) -> None:
        name = form["serviceName"]
        file = form.get("configFile") or DEFAULT_FILE
        route = {
            "rule": form["httpRule"],
            "eps": [e for e in form["entryPoints"].split(",") if e],
            "upstream": (
                f"{form.get('scheme', 'http')}://{form['targetIp']}:{form['targetPort']}"
                if form.get("targetIp")
                else "http://shared:80"
            ),
        }
        original = form.get("originalId", "")
        # An existing disabled record under this id stays parked (edited in place).
        if form.get("isEdit") == "true" and original in self.disabled:
            self.disabled[original]["route"] = route
            return
        self.files.setdefault(file, {})[name] = route
