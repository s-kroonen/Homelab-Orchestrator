"""``orchestrator.registry.access_log`` — the common (extended CLF) and JSON
access-log parsers, and the "never crash on garbage" guarantee."""

from __future__ import annotations

import json

from orchestrator.registry.access_log import parse_line, parse_lines

_COMMON_LINE = (
    '10.0.0.1 - - [10/Jun/2026:08:42:00 +0000] "GET /app HTTP/1.1" 200 1337 '
    '"-" "-" 1 "my-router@docker" "http://10.0.0.2:80" 1ms'
)


def _json_line(**overrides: object) -> str:
    base = {
        "ClientHost": "203.0.113.5",
        "StartUTC": "2026-06-10T08:42:00Z",
        "RouterName": "my-router@docker",
        "RequestPath": "/app",
        "DownstreamStatus": 200,
    }
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# common (extended CLF)
# ---------------------------------------------------------------------------


def test_parse_common_line() -> None:
    entry = parse_line(_COMMON_LINE)
    assert entry is not None
    assert entry.client_ip == "10.0.0.1"
    assert entry.router == "my-router@docker"
    assert entry.status == 200
    assert entry.path == "/app"
    assert entry.timestamp.year == 2026
    assert entry.timestamp.month == 6
    assert entry.raw == _COMMON_LINE


def test_parse_common_line_with_different_status_and_router() -> None:
    line = _COMMON_LINE.replace("200", "404", 1).replace("my-router@docker", "other@file")
    entry = parse_line(line)
    assert entry is not None
    assert entry.status == 404
    assert entry.router == "other@file"


# ---------------------------------------------------------------------------
# json
# ---------------------------------------------------------------------------


def test_parse_json_line() -> None:
    entry = parse_line(_json_line())
    assert entry is not None
    assert entry.client_ip == "203.0.113.5"
    assert entry.router == "my-router@docker"
    assert entry.status == 200
    assert entry.path == "/app"
    assert entry.timestamp.year == 2026


def test_parse_json_line_falls_back_to_client_addr() -> None:
    line = _json_line(ClientHost="", ClientAddr="198.51.100.7:54321")
    entry = parse_line(line)
    assert entry is not None
    assert entry.client_ip == "198.51.100.7"


def test_parse_json_line_falls_back_to_origin_status() -> None:
    line = _json_line(DownstreamStatus=None, OriginStatus=502)
    entry = parse_line(line)
    assert entry is not None
    assert entry.status == 502


# ---------------------------------------------------------------------------
# never crashes on garbage
# ---------------------------------------------------------------------------


def test_parse_line_returns_none_for_blank_line() -> None:
    assert parse_line("") is None
    assert parse_line("   \n") is None


def test_parse_line_returns_none_for_truncated_common_line() -> None:
    assert parse_line("10.0.0.1 - - [not even close to a real line") is None


def test_parse_line_returns_none_for_malformed_json() -> None:
    assert parse_line('{"ClientHost": "1.2.3.4", not valid json') is None


def test_parse_line_returns_none_for_json_missing_required_fields() -> None:
    assert parse_line(json.dumps({"ClientHost": "1.2.3.4"})) is None


def test_parse_line_returns_none_for_json_array_not_object() -> None:
    assert parse_line(json.dumps(["not", "an", "object"])) is None


def test_parse_line_returns_none_for_unparseable_timestamp() -> None:
    assert parse_line(_json_line(StartUTC="not-a-timestamp")) is None
    bad_common = _COMMON_LINE.replace("10/Jun/2026:08:42:00 +0000", "garbage-timestamp")
    assert parse_line(bad_common) is None


def test_parse_lines_skips_garbage_and_keeps_the_rest() -> None:
    lines = [
        _COMMON_LINE,
        "this is not a log line at all",
        _json_line(),
        "",
        '{"broken": ',
    ]
    entries = parse_lines(lines)
    assert len(entries) == 2
    assert entries[0].client_ip == "10.0.0.1"
    assert entries[1].client_ip == "203.0.113.5"
