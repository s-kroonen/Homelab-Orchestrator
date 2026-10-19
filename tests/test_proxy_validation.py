"""The proxy-host validator: pure functions shared by the API, the CLI and the
loader. Every failure is a ``{field: message}`` entry."""

from __future__ import annotations

import pytest

from orchestrator.adapters.proxy.base import EntryPointInfo
from orchestrator.registry.proxy_validation import (
    normalize_hostname,
    parse_entry_points,
    resolve_default_entry_points,
    validate_cert_resolver,
    validate_entry_points,
    validate_proxy_host,
    validate_upstream,
)

LIVE = [
    EntryPointInfo("web", ":80"),
    EntryPointInfo("websecure", ":443"),
    EntryPointInfo("traefik", ":8080"),
]


# -- hostname -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("App.Example.COM", "app.example.com"),
        ("  app.example.com.  ", "app.example.com"),
        ("*.example.com", "*.example.com"),
        ("nas", "nas"),
    ],
)
def test_hostname_is_lowercased_and_trimmed(raw: str, expected: str) -> None:
    assert normalize_hostname(raw) == expected


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("", "required"),
        ("https://app.example.com", "scheme"),
        ("app.example.com/path", "path"),
        ("app.example.com:8080", "port"),
        ("app example.com", "spaces"),
        ("-bad.example.com", "not a valid hostname"),
        ("a..b.example.com", "not a valid hostname"),
        ("exa_mple.com", "not a valid hostname"),
    ],
)
def test_hostname_rejects_with_a_reason(raw: str, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        normalize_hostname(raw)


# -- upstream -----------------------------------------------------------------


def test_upstream_accepts_http_and_https_with_port() -> None:
    assert validate_upstream("http://10.0.0.5:8080") == "http://10.0.0.5:8080"
    assert validate_upstream("https://nas.lan:5001") == "https://nas.lan:5001"


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("", "required"),
        ("10.0.0.5:8080", "needs a scheme"),
        ("http://10.0.0.5", "missing a port"),
        ("ftp://10.0.0.5:21", "not supported"),
        ("http://:8080", "no host"),
        ("http://10.0.0.5:99999", "not a valid URL"),
        ("http://10.0.0.5: 80", "spaces"),
    ],
)
def test_upstream_rejects_with_a_friendly_reason(raw: str, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        validate_upstream(raw)


# -- entry points -------------------------------------------------------------


def test_unknown_entry_point_message_names_it_and_lists_the_valid_ones() -> None:
    msg = validate_entry_points(["https"], LIVE)
    assert (
        msg
        == 'Entry point "https" does not exist on this Traefik. Available: web, websecure, traefik'
    )


def test_several_unknown_entry_points_are_all_named() -> None:
    msg = validate_entry_points(["https", "http"], LIVE)
    assert msg is not None and '"https", "http"' in msg and "do not exist" in msg


def test_internal_traefik_entry_point_is_refused() -> None:
    msg = validate_entry_points(["traefik"], LIVE)
    assert msg is not None and "internal" in msg
    # ...even when the live list could not be read.
    assert validate_entry_points(["traefik"], None) is not None


def test_empty_selection_is_refused() -> None:
    assert "at least one" in (validate_entry_points([], LIVE) or "")
    assert "Available: web, websecure, traefik" in (validate_entry_points([], LIVE) or "")


def test_valid_selection_passes_with_and_without_a_live_list() -> None:
    assert validate_entry_points(["websecure", "web"], LIVE) is None
    assert validate_entry_points(["anything-goes"], None) is None  # shape only


def test_entry_point_names_must_look_like_names() -> None:
    assert validate_entry_points(["web secure"], None) is not None
    assert validate_entry_points(["we;b"], None) is not None


def test_parse_entry_points_accepts_csv_and_lists_and_dedupes() -> None:
    assert parse_entry_points("web, websecure,,web") == ["web", "websecure"]
    assert parse_entry_points([" a ", "b", "a"]) == ["a", "b"]
    assert parse_entry_points(None) == []


# -- cert resolver ------------------------------------------------------------


def test_cert_resolver_checked_against_the_known_list_only_when_known() -> None:
    assert validate_cert_resolver("", ["letsencrypt"]) is None
    assert validate_cert_resolver("letsencrypt", ["letsencrypt"]) is None
    assert 'Certificate resolver "nope"' in (validate_cert_resolver("nope", ["letsencrypt"]) or "")
    assert validate_cert_resolver("anything", None) is None  # list unknown -> trust the operator
    assert validate_cert_resolver("bad name!", None) is not None


# -- default selection --------------------------------------------------------


def test_default_prefers_the_configured_setting_when_valid() -> None:
    assert resolve_default_entry_points(["web"], LIVE) == ["web"]


def test_default_ignores_a_configured_value_that_does_not_exist() -> None:
    assert resolve_default_entry_points(["https"], LIVE) == ["websecure"]


def test_default_is_websecure_when_present() -> None:
    assert resolve_default_entry_points([], LIVE) == ["websecure"]


def test_default_is_the_first_non_traefik_entry_point_otherwise() -> None:
    live = [
        EntryPointInfo("traefik", ":8080"),
        EntryPointInfo("https-in", ":443"),
        EntryPointInfo("plain", ":80"),
    ]
    assert resolve_default_entry_points([], live) == ["https-in"]


def test_default_is_empty_when_only_the_internal_one_exists() -> None:
    assert resolve_default_entry_points([], [EntryPointInfo("traefik", ":8080")]) == []


def test_default_trusts_the_setting_when_the_live_list_is_unreadable() -> None:
    assert resolve_default_entry_points(["websecure"], None) == ["websecure"]
    assert resolve_default_entry_points([], None) == []


# -- whole record -------------------------------------------------------------


def test_validate_proxy_host_reports_every_field_at_once() -> None:
    errors = validate_proxy_host(
        hostname="https://x",
        upstream="http://10.0.0.5",
        entry_points=["https"],
        cert_resolver="nope",
        known_entry_points=LIVE,
        known_cert_resolvers=["letsencrypt"],
    )
    assert set(errors) == {"hostname", "upstream", "entry_points", "cert_resolver"}


def test_validate_proxy_host_valid() -> None:
    assert (
        validate_proxy_host(
            hostname="App.example.com",
            upstream="http://10.0.0.5:80",
            entry_points=["websecure"],
            cert_resolver="letsencrypt",
            known_entry_points=LIVE,
            known_cert_resolvers=["letsencrypt"],
        )
        == {}
    )


def test_validate_proxy_host_duplicate_hostname() -> None:
    errors = validate_proxy_host(
        hostname="a.example.com",
        upstream="http://x:1",
        entry_points=["web"],
        taken_hostnames=["A.example.com"],
    )
    assert "already exists" in errors["hostname"]


def test_local_only_hosts_skip_upstream_and_entry_point_checks() -> None:
    assert validate_proxy_host(hostname="a.example.com", live_managed=False) == {}
