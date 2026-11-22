"""The proxy inventory (routers, middlewares, services, transports) and its findings."""

from __future__ import annotations

from types import SimpleNamespace

from orchestrator.registry import proxy_inventory as pi


def _raw() -> dict:
    return {
        "errors": {},
        "routes": [
            {
                "name": "orchestrator-cloud-example-lan",
                "id": "nextcloud.yml::orchestrator-cloud-example-lan",
                "rule": "Host(`cloud.example.lan`)",
                "configFile": "nextcloud.yml",
                "protocol": "http",
            },
            {
                "name": "panel-example-lan",
                "id": "panel.yml::panel-example-lan",
                "rule": "Host(`panel.example.lan`)",
                "configFile": "panel.yml",
                "protocol": "http",
            },
            {
                "name": "app-write",
                "rule": "Host(`app.example.lan`) && Method(`POST`)",
                "configFile": "app.yml",
            },
            {"name": "app-read", "rule": "Host(`app.example.lan`)", "configFile": "app.yml"},
            {
                "name": "wiki-old",
                "rule": "Host(`wiki-old.example.lan`)",
                "configFile": "_shared.yml",
                "enabled": False,
                "middlewares": [],
            },
            {"name": "media", "rule": "Host(`media.example.lan`)", "configFile": "_shared.yml"},
            {
                "name": "orchestrator-media-example-lan",
                "rule": "Host(`media.example.lan`)",
                "configFile": "media.yml",
            },
        ],
        "file_middlewares": [
            {"name": "redirect-to-https", "configFile": "_shared.yml", "type": "http"},
            {"name": "npm-force-ssl", "configFile": "_shared.yml", "type": "http"},
            {"name": "maintenance-nextcloud", "configFile": "_shared.yml", "type": "http"},
            {"name": "maintenance-oldsvc", "configFile": "_shared.yml", "type": "http"},
            {"name": "crowdsec", "configFile": "_shared.yml", "type": "http"},
        ],
        "runtime_routers": [
            {
                "name": "orchestrator-cloud-example-lan@file",
                "provider": "file",
                "rule": "Host(`cloud.example.lan`)",
                "service": "orchestrator-cloud-example-lan-service",
                "tls": {"certResolver": "le"},
                "middlewares": ["maintenance-nextcloud@file"],
                "status": "enabled",
                "entryPoints": ["websecure"],
            },
            {
                "name": "orchestrator-cloud-example-lan-redirect@file",
                "provider": "file",
                "rule": "Host(`cloud.example.lan`)",
                "service": "orchestrator-cloud-example-lan-service",
                "middlewares": ["redirect-to-https@file"],
                "status": "enabled",
            },
            {
                "name": "panel-example-lan@file",
                "provider": "file",
                "rule": "Host(`panel.example.lan`)",
                "service": "panel-failover",
                "tls": {},
                "middlewares": ["npm-force-ssl@file"],
                "status": "enabled",
            },
            {
                "name": "app-write@file",
                "provider": "file",
                "rule": "Host(`app.example.lan`) && Method(`POST`)",
                "service": "app-write",
                "tls": {},
                "status": "enabled",
                "priority": 20,
            },
            {
                "name": "app-read@file",
                "provider": "file",
                "rule": "Host(`app.example.lan`)",
                "service": "app-read",
                "tls": {},
                "status": "enabled",
            },
            {
                "name": "media@file",
                "provider": "file",
                "rule": "Host(`media.example.lan`)",
                "service": "media",
                "tls": {},
                "status": "enabled",
            },
            {
                "name": "orchestrator-media-example-lan@file",
                "provider": "file",
                "rule": "Host(`media.example.lan`)",
                "service": "media",
                "tls": {},
                "status": "enabled",
            },
            {
                "name": "portainer@docker",
                "provider": "docker",
                "rule": "Host(`port.example.lan`)",
                "service": "portainer",
                "status": "enabled",
            },
            {
                "name": "dashboard@internal",
                "provider": "internal",
                "rule": "PathPrefix(`/api`)",
                "service": "api@internal",
                "status": "enabled",
            },
        ],
        "runtime_middlewares": [
            {
                "name": "redirect-to-https@file",
                "provider": "file",
                "type": "redirectscheme",
                "redirectScheme": {"scheme": "https"},
                "usedBy": ["orchestrator-cloud-example-lan-redirect@file"],
            },
            {
                "name": "npm-force-ssl@file",
                "provider": "file",
                "type": "redirectScheme",
                "redirectScheme": {"scheme": "https"},
                "usedBy": ["panel-example-lan@file"],
            },
            {
                "name": "maintenance-nextcloud@file",
                "provider": "file",
                "type": "errors",
                "errors": {
                    "status": ["502-504"],
                    "service": "orchestrator-maintenance-backend-service",
                },
            },
            {
                "name": "maintenance-oldsvc@file",
                "provider": "file",
                "type": "errors",
                "errors": {
                    "status": ["502-504"],
                    "service": "orchestrator-maintenance-backend-service",
                },
            },
            {
                "name": "crowdsec@file",
                "provider": "file",
                "type": "plugin",
                "plugin": {"bouncer": {}},
            },
        ],
        "runtime_services": [
            {
                "name": "orchestrator-cloud-example-lan-service@file",
                "provider": "file",
                "loadBalancer": {"servers": [{"url": "http://10.0.1.11:80"}]},
                "serverStatus": {"http://10.0.1.11:80": "UP"},
            },
            {
                "name": "panel-failover@file",
                "provider": "file",
                "failover": {"service": "panel-stack1", "fallback": "panel-stack2"},
            },
            {
                "name": "panel-stack1@file",
                "provider": "file",
                "loadBalancer": {
                    "servers": [{"url": "http://10.0.1.14:80"}],
                    "healthCheck": {"path": "/auth/login"},
                },
                "serverStatus": {"http://10.0.1.14:80": "DOWN"},
            },
            {
                "name": "panel-stack2@file",
                "provider": "file",
                "loadBalancer": {
                    "servers": [{"url": "https://panel.remote.example.lan"}],
                    "healthCheck": {"path": "/auth/login"},
                    "passHostHeader": False,
                    "serversTransport": "panel-remote",
                },
            },
            {
                "name": "app-write@file",
                "provider": "file",
                "failover": {"service": "app-remote", "fallback": "app-standby-raw"},
            },
            {
                "name": "app-read@file",
                "provider": "file",
                "failover": {"service": "app-standby", "fallback": "app-remote"},
            },
            {
                "name": "app-remote@file",
                "provider": "file",
                "loadBalancer": {
                    "servers": [{"url": "https://app.remote.example.lan"}],
                    "healthCheck": {"path": "/h"},
                },
            },
            {
                "name": "app-standby@file",
                "provider": "file",
                "loadBalancer": {
                    "servers": [{"url": "http://10.0.1.12:2999"}],
                    "healthCheck": {"path": "/h"},
                },
            },
            {
                "name": "app-standby-raw@file",
                "provider": "file",
                "loadBalancer": {"servers": [{"url": "http://10.0.1.12:2999"}]},
            },
            {
                "name": "media@file",
                "provider": "file",
                "loadBalancer": {"servers": [{"url": "http://10.0.1.16:8096"}]},
            },
            {
                "name": "legacy-backend@file",
                "provider": "file",
                "loadBalancer": {"servers": [{"url": "http://10.0.1.40:80"}]},
            },
            {
                "name": "orchestrator-maintenance-backend-service@file",
                "provider": "file",
                "loadBalancer": {"servers": [{"url": "http://10.0.0.5:8000"}]},
            },
            {
                "name": "portainer@docker",
                "provider": "docker",
                "loadBalancer": {"servers": [{"url": "http://172.18.0.4:9000"}]},
            },
        ],
        "managed_services": ["panel-failover"],
        "entrypoint_middlewares": ["crowdsec@file"],
        "config_errors": [],
    }


HOSTS = [
    SimpleNamespace(hostname="cloud.example.lan", id=1),
    SimpleNamespace(hostname="panel.example.lan", id=2),
    SimpleNamespace(hostname="app.example.lan", id=3),
    SimpleNamespace(hostname="media.example.lan", id=4),
]


def _inv(transports=None):
    return pi.build(
        _raw(),
        hosts=HOSTS,
        owns=lambda n: n.startswith("orchestrator-"),
        transports=transports,
    )


def test_routers_carry_provider_file_maker_and_host():
    inv = _inv()
    by = {r.name: r for r in inv.routers}
    cloud = by["orchestrator-cloud-example-lan@file"]
    assert (cloud.made_by, cloud.config_file, cloud.proxy_host) == (
        "orchestrator",
        "nextcloud.yml",
        "cloud.example.lan",
    )
    assert by["panel-example-lan@file"].made_by == "hand-written"
    assert by["portainer@docker"].made_by == "docker"
    assert by["dashboard@internal"].made_by == "internal"
    assert by["orchestrator-cloud-example-lan-redirect@file"].redirect is True
    # A route switched off in Traefik Manager is listed although Traefik no longer runs it.
    off = by["wiki-old@file"]
    assert (off.status, off.proxy_host) == ("off", "")
    assert pi.goes_to(inv, by["panel-example-lan@file"]) == (
        "failover: http://10.0.1.14:80 -> https://panel.remote.example.lan"
    )


def test_backends_used_by_and_managed():
    inv = _inv()
    by = {b.name: b for b in inv.backends}
    assert by["panel-stack1@file"].used_by == ["panel-failover@file"]
    assert by["panel-failover@file"].managed_by_tm is True
    assert by["orchestrator-maintenance-backend-service@file"].used_by == [
        "maintenance-nextcloud@file",
        "maintenance-oldsvc@file",
    ]
    assert by["panel-stack1@file"].servers == [{"url": "http://10.0.1.14:80", "status": "DOWN"}]


def test_middlewares_used_by_and_entrypoint():
    inv = _inv()
    by = {m.name: m for m in inv.middlewares}
    assert by["maintenance-nextcloud@file"].used_by == ["orchestrator-cloud-example-lan@file"]
    assert by["maintenance-nextcloud@file"].made_by == "orchestrator"
    assert by["crowdsec@file"].on_entrypoint is True
    assert by["maintenance-nextcloud@file"].summary == "errors · 502-504"


def test_transports_named_only_without_the_traefik_api():
    inv = _inv()
    assert [(t.name, t.known, t.used_by) for t in inv.transports] == [
        ("panel-remote", False, ["panel-stack2@file"])
    ]
    inv = _inv(
        {
            "panel-remote@file": {"serverName": "panel.remote.example.lan"},
            "spare@file": {"insecureSkipVerify": True},
        }
    )
    by = {t.name: t for t in inv.transports}
    assert by["panel-remote"].server_name == "panel.remote.example.lan"
    assert by["spare"].used_by == []


def _review_raw() -> dict:
    """The example proxy plus what only the review looks at."""
    raw = _raw()
    raw["config_errors"] = [{"file": "vpn.yml", "error": "mapping values are not allowed here"}]
    raw["runtime_tcp_routers"] = [
        {"name": "mqtt-tls@file", "rule": "HostSNI(`mqtt.example.lan`)", "provider": "file"}
    ]
    raw["tls_options"] = [{"name": "modern", "configFile": "_shared.yml"}]
    raw["entrypoint_redirects"] = {}
    raw["runtime_routers"] += [
        {
            "name": "docs@file",
            "provider": "file",
            "rule": "HostRegexp(`^docs\\..+$`)",
            "service": "docs-service",
            "status": "enabled",
        },
        {
            "name": "bingo@file",
            "provider": "file",
            "rule": "Host(`bingo.example.lan`)",
            "service": "bingo-service",
            "tls": {},
            "status": "enabled",
            "middlewares": ["maintenance-portainer@file", "maintenance-bingo@file"],
        },
        {
            "name": "web-old@file",
            "provider": "file",
            "rule": "Host(`web-old.example.lan`)",
            "service": "web-old-service",
            "tls": {},
            "status": "warning",
            "error": ['the service "web-old-service@file" does not exist'],
        },
    ]
    raw["runtime_middlewares"] += [
        {
            "name": "maintenance-portainer@file",
            "provider": "file",
            "errors": {
                "status": ["502-504"],
                "service": "orchestrator-maintenance-backend-service",
            },
        },
        {
            "name": "maintenance-bingo@file",
            "provider": "file",
            "errors": {
                "status": ["502-504"],
                "service": "orchestrator-maintenance-backend-service",
            },
        },
        {
            "name": "cloud-hsts@file",
            "provider": "file",
            "headers": {"stsSeconds": 31536000},
            "usedBy": ["orchestrator-cloud-example-lan@file"],
        },
        {
            "name": "media-hsts@file",
            "provider": "file",
            "headers": {"stsSeconds": 31536000},
            "usedBy": ["media@file"],
        },
    ]
    raw["runtime_services"] += [
        {"name": "api-hrw@file", "provider": "file", "highestRandomWeight": {"services": []}},
        {
            "name": "bingo-service@file",
            "provider": "file",
            "loadBalancer": {"servers": [{"url": "http://10.0.1.12:8085"}]},
        },
    ]
    return raw


def _review(**kw):
    inv = pi.build(
        _review_raw(),
        hosts=[*HOSTS, SimpleNamespace(hostname="bingo.example.lan", id=5)],
        owns=lambda n: n.startswith("orchestrator-"),
        transports=kw.pop("transports", None),
    )
    return inv, {f.id: f for f in pi.findings(inv, service_slugs=["nextcloud", "media"], **kw)}


def test_router_shape_decides_who_made_it():
    inv, _ = _review()
    by = {r.name: r for r in inv.routers}
    assert by["orchestrator-cloud-example-lan@file"].made_by == "orchestrator"
    assert by["bingo@file"].made_by == "Traefik Manager"  # router bingo, service bingo-service
    panel = by["panel-example-lan@file"]
    assert panel.made_by == "hand-written"
    # its failover is one Traefik Manager manages, but the form cannot give its backends
    # different Host headers
    assert any("different Host headers" in w for w in panel.not_form)
    assert by["media@file"].not_form == ["service media is not media-service"]


def test_review_errors_and_what_is_not_read_yet():
    _, found = _review()
    assert found["error:parse:vpn.yml"].fix == "Fix the YAML in Traefik Manager's Raw editor."
    assert "does not exist" in found["error:router:web-old"].detail
    assert found["unread:tcp:mqtt-tls@file"].target == "mqtt-tls"
    assert "highestRandomWeight" in found["unread:hrw:api-hrw"].detail
    assert "no hostname can be read" in found["unread:rule:docs"].detail
    assert found["unread:tls_options"].target == "TLS options: modern"


def test_review_hand_written_says_the_form_version_or_stays():
    _, found = _review()
    panel = found["handwritten:panel.example.lan"]
    assert panel.fix.startswith("Stays hand-written")
    app = found["handwritten:app.example.lan"]
    assert app.detail.startswith("2 routers (app-read, app-write priority 20)")
    assert app.fix == (
        "Traefik Manager's form: 2 routes on the host, each with its own rule and priority, "
        "with failover or weighted backends; then Import or Adopt it here."
    )
    assert app.file == "app.yml"
    assert "handwritten:cloud.example.lan" not in found  # the orchestrator's own
    assert "handwritten:bingo.example.lan" not in found  # Traefik Manager's form


def test_review_clean_up():
    _, found = _review(
        transports={"panel-remote@file": {"serverName": "x"}, "spare@file": {}},
        service_of={"bingo.example.lan": "bingo"},
    )
    red = found["cleanup:redirect_routers"]
    assert "while the web entry point has no redirect" in red.detail
    double = found["cleanup:double_maintenance:bingo.example.lan"]
    assert double.fix == "Keep only maintenance-bingo, the service it is linked to."
    copies = found["cleanup:copies:headers:cloud-hsts"]
    assert copies.detail == "2 headers middlewares with the same settings: cloud-hsts, media-hsts."
    assert found["cleanup:unused:middleware:maintenance-oldsvc"].detail.startswith(
        "Maintenance page for a service"
    )
    assert "cleanup:unused:middleware:crowdsec" not in found  # an entry point applies it
    assert "cleanup:unused:service:legacy-backend" in found
    assert "cleanup:unused:transport:spare" in found
    assert "Switched off (wiki-old.example.lan)" in found["cleanup:off:_shared.yml"].detail
    assert "cleanup:duplicate:media+orchestrator-media-example-lan" in found
    assert found["cleanup:https_redirect:npm-force-ssl"].refs["routers"] == [
        "panel-example-lan@file"
    ]
    assert "cleanup:fallback_unchecked:app-write" in found
    assert found["cleanup:shared_file:_shared.yml"].refs["routers"] == ["media@file"]


def test_redirect_routers_when_the_entry_point_already_redirects():
    raw = _review_raw()
    raw["entrypoint_redirects"] = {"web": "websecure"}
    raw["runtime_routers"][1]["entryPoints"] = ["web"]
    raw["runtime_routers"][1]["middlewares"].append("maintenance-nextcloud@file")
    inv = pi.build(raw, hosts=HOSTS, owns=lambda n: n.startswith("orchestrator-"))
    found = {f.id: f for f in pi.findings(inv)}
    red = found["cleanup:redirect_routers"]
    assert "already redirects" in red.detail and red.fix == "Delete them in Traefik Manager."
    assert found["cleanup:redirect_maintenance"].refs["routers"] == [
        "orchestrator-cloud-example-lan-redirect@file"
    ]


def test_report_and_markdown_say_what_is_supported():
    inv, found = _review()
    rep = pi.report(inv, list(found.values()))
    assert rep["counts"]["error"] == 2 and rep["counts"]["findings"] == len(found)
    assert {"what": "TCP and UDP routers", "status": "not yet"} in rep["support"]
    md = pi.to_markdown(rep, source="gateway")
    assert "## What the orchestrator supports" in md
    assert "| TCP and UDP routers | not yet |" in md
    assert "## Errors (2)" in md
    assert "| What | Finding | File | How to fix |" in md
    assert "Entry point redirects: none" in md
    assert '"review"' in pi.to_json(inv, rep) and "decision" not in md


def test_a_part_that_could_not_be_read_is_named():
    raw = _raw()
    raw["runtime_middlewares"] = None
    raw["errors"] = {"middlewares": "Traefik Manager timed out"}
    inv = pi.build(raw, hosts=HOSTS)
    assert inv.middlewares == []
    assert inv.errors["middlewares"] == "Traefik Manager timed out"
    assert inv.errors["runtime_middlewares"] == "could not be read"
