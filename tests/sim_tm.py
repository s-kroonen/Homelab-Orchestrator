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
  creates a second copy in the default file. A ``configFile`` TM does not know
  is created when it runs with ``CONFIG_DIR`` (``config_dir=True``) and is
  SILENTLY replaced by the default file otherwise (``resolve_config_path``
  returning ``''``). An edit whose ``originalId`` carries ``<old>::`` and a
  different ``configFile`` MOVES the route (the router is removed from ``<old>``).
* ``GET /api/configs`` lists the config files and ``configDirSet``;
  ``GET /api/settings`` carries ``cert_resolver``; ``GET /api/static/config``
  answers the parsed static config (``static_resolvers``) or 404 when TM has no
  static config path; ``GET /api/traefik/routers`` shows ``tls.certResolver``.

``ignore_toggle`` makes toggles do nothing at all (a 200 that lied).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, unquote

import httpx

DEFAULT_FILE = "dynamic.yml"


class SimTM:
    def __init__(
        self,
        files: tuple[str, ...] = (DEFAULT_FILE, "apps.yml"),
        multi: bool = True,
        *,
        config_dir: bool = False,
        resolvers: str = "",
        static_resolvers: list[str] | None = None,
        static_status: int = 200,
        runtime_only_resolvers: tuple[str, ...] = (),
        settings_status: int = 200,
        configs_status: int = 200,
        middleware_names: tuple[str, ...] = ("redirect-to-https@file",),
    ):
        self.middleware_names = middleware_names
        self.created_middlewares: dict[str, str] = {}
        self.multi = multi
        self.config_dir = config_dir
        self.resolvers = resolvers
        self.static_resolvers = static_resolvers
        self.static_status = static_status
        self.runtime_only_resolvers = runtime_only_resolvers
        self.settings_status = settings_status
        self.configs_status = configs_status
        self.leak_on_move = False  # a move that leaves the old copy behind
        self.files: dict[str, dict[str, dict[str, Any]]] = {f: {} for f in files}
        self.disabled: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.ignore_toggle = False
        self.toggle_requests: list[httpx.Request] = []
        #: router names Traefik reports as errored at runtime (a rewrite it refused)
        self.runtime_errors: set[str] = set()
        #: route ids whose delete answers 500
        self.fail_delete: set[str] = set()

    # -- helpers -------------------------------------------------------------

    def add(
        self,
        file: str,
        name: str,
        hostname: str,
        *,
        eps: tuple[str, ...] = ("websecure",),
        upstream: str = "http://10.0.0.9:8080",
        middlewares: tuple[str, ...] = (),
        raw: str | None = None,
        resolver: str = "",
        rule: str | None = None,
    ) -> str:
        self.files[file][name] = {
            "rule": rule or f"Host(`{hostname}`)",
            "eps": list(eps),
            "upstream": upstream,
            "mws": list(middlewares),
            "resolver": resolver,
            **({"raw": raw} if raw is not None else {}),
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
            if self.settings_status != 200:
                return httpx.Response(self.settings_status, json={"error": "nope"})
            return httpx.Response(
                200, json={"cert_resolver": self.resolvers, "domains": ["example.com"]}
            )
        if path == "/api/configs":
            if self.configs_status != 200:
                return httpx.Response(self.configs_status, json={"error": "nope"})
            return httpx.Response(
                200,
                json={
                    "files": [
                        {"label": f, "path": f"/etc/traefik/dynamic/{f}"} for f in self.files
                    ],
                    "configDirSet": self.config_dir,
                },
            )
        if path == "/api/static/config":
            if self.static_resolvers is None or self.static_status != 200:
                status = self.static_status if self.static_status != 200 else 404
                return httpx.Response(
                    status, json={"error": "Static config not found or STATIC_CONFIG_PATH not set"}
                )
            return httpx.Response(
                200,
                json={
                    "raw": "",
                    "parsed": {"certificatesResolvers": {n: {} for n in self.static_resolvers}},
                    "path": "/etc/traefik/traefik.yml",
                },
            )
        if path == "/api/traefik/middlewares":
            return httpx.Response(
                200, json={"http": [{"name": name} for name in self.middleware_names]}
            )
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
        if path.startswith("/api/routes/") and path.endswith("/raw"):
            route_id = path.removeprefix("/api/routes/").removesuffix("/raw")
            plain = route_id.split("::", 1)[-1]
            file = self.locate(plain, route_id.split("::", 1)[0] if "::" in route_id else "")
            if file is None:
                return httpx.Response(404, json={"error": "not found"})
            route = self.files[file][plain]
            if method == "POST":
                route["raw"] = json.loads(request.read())["content"]
                self.requests.append(("raw", route_id, route["raw"]))
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(
                200, json={"raw": self.raw_of(plain, route), "configFile": file, "proto": "http"}
            )
        if method == "POST" and path.startswith("/delete/"):
            route_id = path.removeprefix("/delete/")
            if route_id in self.fail_delete or route_id.split("::", 1)[-1] in self.fail_delete:
                return httpx.Response(500, json={"error": "boom"})
            form = dict(parse_qsl(request.read().decode(), keep_blank_values=True))
            self.requests.append(("delete", route_id, form.get("configFile", "")))
            return self._delete(route_id, form.get("configFile", ""))
        if method == "POST" and path == "/save-middleware":
            form = dict(parse_qsl(request.read().decode(), keep_blank_values=True))
            self.requests.append(("save-middleware", form["middlewareName"], form))
            self.created_middlewares[form["middlewareName"]] = form["middlewareContent"]
            self.middleware_names = (*self.middleware_names, f"{form['middlewareName']}@file")
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path == "/save":
            form = dict(parse_qsl(request.read().decode(), keep_blank_values=True))
            self.requests.append(("save", form["serviceName"], form))
            self._save(form)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": True})

    def raw_of(self, name: str, route: dict[str, Any]) -> str:
        if "raw" in route:
            return str(route["raw"])
        mws = "".join(f"\n        - {m}" for m in route.get("mws", []))
        tls = (
            f"\n      tls:\n        certResolver: {route['resolver']}"
            if route.get("resolver")
            else ""
        )
        return (
            "http:\n  routers:\n"
            f"    {name}:\n      rule: {route['rule']}\n      entryPoints: {json.dumps(route['eps'])}\n"
            f"      service: {name}{tls}"
            + (f"\n      middlewares:{mws}" if mws else "")
            + f"\n  services:\n    {name}:\n      loadBalancer:\n        servers:\n"
            f"          - url: {route['upstream']}\n        passHostHeader: true\n"
        )

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
        out = [
            {
                "name": f"{name}@file",
                "rule": r["rule"],
                "service": f"{name}-service@file",
                "entryPoints": r["eps"],
                "status": "enabled",
                **(
                    {"error": ["router refused"], "status": "disabled"}
                    if name in self.runtime_errors
                    else {}
                ),
                **({"tls": {"certResolver": r["resolver"]}} if r.get("resolver") else {}),
            }
            for routes in self.files.values()
            for name, r in routes.items()
        ]
        for i, resolver in enumerate(self.runtime_only_resolvers):
            out.append(
                {
                    "name": f"hand-{i}@file",
                    "rule": f"Host(`hand-{i}.example.com`)",
                    "service": f"hand-{i}@file",
                    "entryPoints": ["websecure"],
                    "status": "enabled",
                    "tls": {"certResolver": resolver},
                }
            )
        return out

    def file_of(self, name: str) -> str | None:
        """The config file that holds router ``name`` (None = nowhere)."""
        return self.locate(name)

    def copies_of(self, name: str) -> list[str]:
        return [f for f, routes in self.files.items() if name in routes]

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

    def _resolve_file(self, wanted: str) -> str:
        """TM's ``resolve_config_path`` + the ``or env.CONFIG_PATH`` fallback."""
        if not wanted:
            return DEFAULT_FILE
        if wanted in self.files:
            return wanted
        if self.config_dir and "/" not in wanted and "\\" not in wanted:
            name = wanted if wanted.endswith((".yml", ".yaml")) else wanted + ".yml"
            self.files.setdefault(name, {})  # TM creates it
            return name
        return DEFAULT_FILE  # silently: the route lands in the DEFAULT file

    def _save(self, form: dict[str, str]) -> None:
        name = form["serviceName"]
        file = self._resolve_file(form.get("configFile", ""))
        cfg_filename = form.get("configFile") or DEFAULT_FILE
        resolver = form.get("certResolver", "")
        route = {
            "rule": form["httpRule"],
            "eps": [e for e in form["entryPoints"].split(",") if e],
            "upstream": (
                f"{form.get('scheme', 'http')}://{form['targetIp']}:{form['targetPort']}"
                if form.get("targetIp")
                else "http://shared:80"
            ),
            "resolver": "" if resolver.startswith("__") else resolver,
            "mws": [m for m in form.get("middlewares", "").split(",") if m],
        }
        original = form.get("originalId", "")
        # An existing disabled record under this id stays parked (edited in place).
        if form.get("isEdit") == "true" and original in self.disabled:
            self.disabled[original]["route"] = route
            return
        if form.get("isEdit") == "true" and "::" in original:
            old_file, plain = original.split("::", 1)
            if old_file != cfg_filename and old_file in self.files and not self.leak_on_move:
                # TM's move: the router leaves the file named in the id prefix.
                self.files[old_file].pop(plain, None)
        self.files.setdefault(file, {})[name] = route
