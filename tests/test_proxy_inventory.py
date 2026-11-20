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


def test_findings():
    inv = _inv({"panel-remote@file": {"serverName": "x"}, "spare@file": {}})
    found = {f.id: f for f in pi.findings(inv, service_slugs=["nextcloud", "media"])}
    assert found["convert:panel.example.lan"].detail == "Hand-written failover route in panel.yml."
    assert found["convert:app.example.lan"].refs["routers"] == ["app-read@file", "app-write@file"]
    assert "convert:cloud.example.lan" not in found  # the orchestrator's own
    assert found["unused:middleware:maintenance-oldsvc"].detail.startswith(
        "Maintenance page for a service"
    )
    assert "unused:middleware:crowdsec" not in found  # an entry point applies it
    assert "unused:middleware:maintenance-nextcloud" not in found
    assert "unused:service:legacy-backend" in found
    assert "unused:service:orchestrator-maintenance-backend-service" not in found
    assert "unused:service:portainer" not in found  # Docker's own come and go
    assert "unused:transport:spare" in found
    assert found["leftover:router:wiki-old"].suggestion == "delete"
    assert "leftover:duplicate:media+orchestrator-media-example-lan" in found
    assert found["old_syntax:npm-force-ssl"].refs["routers"] == ["panel-example-lan@file"]
    assert (
        "old_syntax:redirect-to-https" not in found
    )  # on the HTTP redirect router, where it belongs
    assert found["look:fallback_unchecked:app-write"].suggestion == "keep"
    assert "look:fallback_unchecked:panel-failover" not in found
    assert found["look:shared_file:_shared.yml"].refs["routers"] == ["media@file"]


def test_report_and_markdown_carry_decisions():
    inv = _inv()
    found = pi.findings(inv)
    rep = pi.report(
        inv, found, {"convert:panel.example.lan": {"decision": "convert", "by": "admin"}}
    )
    assert rep["counts"]["decided"] == 1
    md = pi.to_markdown(rep, source="gateway")
    assert "## Can be converted to the standard" in md
    assert (
        "| `panel.example.lan` | Hand-written failover route in panel.yml. | Convert | Convert |"
        in md
    )
    assert '"inventory"' in pi.to_json(inv, rep)


def test_a_part_that_could_not_be_read_is_named():
    raw = _raw()
    raw["runtime_middlewares"] = None
    raw["errors"] = {"middlewares": "Traefik Manager timed out"}
    inv = pi.build(raw, hosts=HOSTS)
    assert inv.middlewares == []
    assert inv.errors["middlewares"] == "Traefik Manager timed out"
    assert inv.errors["runtime_middlewares"] == "could not be read"


def test_decisions_are_stored_per_source(session):
    pi.save_decision(session, "gw", "convert:panel.example.lan", "convert", "admin")
    session.commit()
    assert pi.load_decisions(session, "gw")["convert:panel.example.lan"]["decision"] == "convert"
    assert pi.load_decisions(session, "other") == {}
    try:
        pi.save_decision(session, "gw", "x", "explode", "admin")
    except ValueError as exc:
        assert "decision must be one of" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unknown decision was accepted")
