# GeoIP traffic visibility

Which countries and IPs are hitting this homelab's exposed services, and
against which service — an opt-in companion to
[traffic_metrics.md](traffic_metrics.md)'s per-router request counters,
sourced from a different place: Traefik's own **access log**, not its
aggregate Prometheus counters.

**The dashboard page for this is a deferred follow-up** — see
[roadmap.md](roadmap.md#small-features-each-independently-schedulable-no-architecture-decisions-needed).
This change is ingestion + storage + one bare JSON read endpoint only,
deliberately scoped to avoid colliding with a concurrent nav redesign (see
that roadmap entry for why).

## Why a separate feature from `traffic_metrics.md`

`TrafficCollector` polls `{TRAEFIK_API_URL}/metrics` — Traefik's aggregate,
cumulative-since-process-start Prometheus counters. They have **no
per-request client IPs at all**, by design (that's not what Prometheus
counters are for). The only place a client IP appears is Traefik's access
log, one line per request.

This app has no direct SSH/filesystem access to that log, and deliberately
does not gain any for this feature. Instead it reuses a read Traefik
Manager (the same tool already providing the write path in
[proxy_hosts.md](proxy_hosts.md)) already exposes for its own **Logs**
tab: `GET /api/traefik/logs?lines=N` (capped at 1000 lines), gated on an
access-log path being configured on the gateway. That is
`ProxyAdapter.tail_access_log` (`orchestrator.adapters.proxy.base`),
implemented by `TraefikManagerProxyAdapter` and faked (as `None`, never a
real network call) by `DryRunProxyAdapter`.

## Opt-in, and why

`GEOIP_ENABLED` (default `false`) gates the whole feature — the collector,
the GeoIP database, the read endpoint's real response. Off by default
because:

- It is a brand-new integration nobody has opted into yet, same "off by
  default" convention `HEALTH_SCAN_ENABLED`/`SCHEDULER_ENABLED` already
  follow.
- It needs `TRAEFIK_MANAGER_URL` configured AND an access-log path set on
  the gateway (`ACCESS_LOG_PATH` or Traefik Manager's own Settings) — most
  installs following this repo's existing docs will not have the latter
  yet.
- It downloads and caches a third-party GeoIP database (see below) the
  first time it runs — not something to do silently.

With it off, `ProxyAdapter.tail_access_log` is simply never polled, and
`GET /api/traffic/geo` answers `{"enabled": false, ...empty...}` without
touching the database at all.

## Ingestion path

```
Traefik's access log file
        │  (tailed internally by Traefik Manager for its own Logs tab)
        ▼
Traefik Manager  GET /api/traefik/logs?lines=N
        │  (ProxyAdapter.tail_access_log)
        ▼
orchestrator.scheduler.geo_traffic_collector.GeoTrafficCollector
        │  parse (orchestrator.registry.access_log)
        │  dedupe against a persisted cursor
        │  resolve IP -> country (orchestrator.registry.geoip)
        │  aggregate by (proxy_host, client_ip)
        ▼
GeoTrafficSample rows
        │
        ▼
GET /api/traffic/geo  (read-only; the deferred dashboard page's hook)
```

Nothing here is a new SSH, filesystem, or direct-Traefik-API integration —
it is one more REST call to the same Traefik Manager instance
`TRAEFIK_MANAGER_URL` already points at.

## Access-log format(s) supported

Traefik's access log has exactly two shapes (its own docs, "Logs and
access logs"): `common` (an extension of the Apache/NCSA Combined Log
Format with four extra, Traefik-specific fields appended) — Traefik's own
**default** when `accessLog` is enabled with no `format` given — and
`json`, one object per line under Go field names.

`orchestrator.registry.access_log.parse_line` supports **both** — sniffing
each line (`{` => JSON, otherwise the common-format regex) rather than
requiring the operator to tell this app which one Traefik is configured
with, since that is static config on the gateway this app has no visibility
into otherwise. Supporting both turned out to cost about the same as
supporting one (each parser is a handful of lines), so there was no reason
to pick only one.

A line that matches neither shape (truncated, a future/unknown format,
blank, non-log noise) is reported back as `None`. The collector logs it at
debug level and skips it — **a single garbled line never crashes the
poll**, same fail-open philosophy as every other optional integration in
this app.

## Dedup / cursor strategy

`GET /api/traefik/logs` has **no native "since" parameter** — every poll
re-fetches the same tail (the last `GEOIP_ACCESS_LOG_LINES` lines of the
*current* file), per Traefik Manager's own API docs. Re-polling therefore
re-fetches lines this collector may already have counted; naively counting
every fetched line every time would corrupt the aggregate numbers (the same
risk `TrafficCollector` avoids by storing a raw cumulative counter instead
of a delta — this collector's situation is different because there is no
counter at all here, only a sliding window of raw lines).

The fix: a cursor persisted in the `setting` table
(`geoip_traffic.cursor`, via `orchestrator.db.models.Setting` — the same
table the YAML-reconcile loader already uses for its own bookkeeping
markers) holding:

- `last_ts`: the latest timestamp seen across every *parsed* line in the
  previous poll's fetched tail.
- `last_hashes`: the SHA-256 hashes of every line that shared that exact
  timestamp (ties are possible under load — Traefik's own access-log
  timestamp resolution is not guaranteed sub-line-unique).

A line counts as "new" this poll if its timestamp is strictly after
`last_ts`, or exactly equal to it and its hash isn't in `last_hashes`. The
cursor is then advanced using **every** line in the current poll's fetched
tail (not just the newly-counted ones), so a poll that sees nothing new
still keeps the cursor correct for the next one.

Persisting it (rather than keeping it only in the collector's own memory)
means a container restart does not re-count whatever is still sitting in
the tail window — verified by `tests/test_geo_traffic_collector.py`'s
`test_cursor_is_persisted_in_the_setting_table`, and the double-counting
regression itself by `test_repolling_the_same_tail_does_not_double_count` /
`test_repolling_an_overlapping_but_grown_tail_only_counts_new_lines`.

**Accepted gap, not a bug:** if more than `GEOIP_ACCESS_LOG_LINES` new
lines land between two polls, the oldest of them fall outside the tail
entirely and are permanently missed — there is no way to recover them
without reading the file directly, which is deliberately out of scope (see
`ProxyAdapter.tail_access_log`'s docstring). This mirrors
`TrafficCollector`'s own accepted "up to one poll interval of latency"
trade-off: good enough for trend visibility, not a guarantee of exact
counts.

## Local/private IPs get their own bucket

A homelab's exposed services see plenty of RFC 1918 / loopback /
link-local / CGNAT / multicast traffic (the LAN itself, health checks,
internal probes) that no public GeoIP database can resolve to a country —
and that is expected, not a lookup failure. `orchestrator.registry.geoip`
buckets these explicitly as `"local"` (via Python's own `ipaddress`
module: `is_private`/`is_loopback`/`is_link_local`/`is_multicast`/
`is_reserved`/`is_unspecified`), distinct from `"unknown"` (a genuinely
public IP the configured database just doesn't cover). This check runs
**before** any database lookup, so it works even with GeoIP otherwise
unavailable.

## GeoIP database

`GEOIP_DB_PATH` points at a ready-made MaxMind-format (`.mmdb`) file. Left
empty (the default), `orchestrator.registry.geoip.GeoIP` downloads and
caches the free **DB-IP city-lite** database under `{STATE_DIR}/geoip/` —
the same database Traefik Manager's own GeoIP feature downloads
(`POST /api/geoip/update` in its API docs) when no custom database is
configured, rather than this app inventing a different source or
dependency. The cache is keyed by calendar month (DB-IP republishes
monthly); a stale cached file from a previous month is still used if a
fresh download fails, rather than going dark for a transient network
hiccup. A `GEOIP_DB_PATH` that is set but does not exist on disk is
**not** auto-downloaded over — that would silently override an explicit
operator choice — it is logged and GeoIP resolves as `"unknown"` until
fixed.

Resolution uses [`geoip2`](https://pypi.org/project/geoip2/), the standard
Python client for MaxMind-format databases (DB-IP's own files are
MaxMind-DB-compatible). `GeoIP` detects whether the configured database is
city-capable (via its own metadata) and calls `.city()` for a city name too
when it is, falling back to `.country()` otherwise.

## Storage

`GeoTrafficSample` (`orchestrator.db.models`) — the GeoIP companion to
`TrafficSample`, same compact/short-retention philosophy as
`HealthScanSample`: one row per `(proxy_host, client_ip)` pair actually
seen in a poll cycle, with `hit_count` holding how many *new* access-log
lines (since the cursor) matched it, plus the resolved `bucket` /
`country_code` / `country_name` / `city` and the most recent `status_code`
observed for that pair this cycle. This is a monitoring trend view
("which countries/IPs hit us, how often, against which service"), not a
forensic per-request log — the raw lines already live in Traefik's own
access-log file; this does not duplicate them.

Only entries whose router resolves (via the same `fetch_router_details`
hostname lookup `TrafficCollector` already uses) to an existing
`ProxyHost` are stored — a router with no matching `ProxyHost` has no
`Service` to attribute the hit to, same reasoning `TrafficCollector` gives
for skipping those.

Migration: `0008_geo_traffic_sample` (`down_revision =
"0007_health_scan_sample"`, the actual current head at the time this was
written).

## Retention

`GEOIP_TRAFFIC_RETENTION_DAYS` (default **14**, matching
`TRAEFIK_METRICS_RETENTION_DAYS`) — pruned every poll, same pattern as
every other collector here, independent of whether that particular poll
got anything new.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `GEOIP_ENABLED` | `false` | Gates the whole feature. |
| `GEOIP_DB_PATH` | `""` | A ready-made `.mmdb` file. Empty = auto-download/cache the free DB-IP city-lite database. |
| `GEOIP_TRAFFIC_POLL_INTERVAL_S` | `60` | How often the collector tails the access log. |
| `GEOIP_ACCESS_LOG_LINES` | `200` | Lines requested per poll (Traefik Manager caps this at 1000). |
| `GEOIP_TRAFFIC_RETENTION_DAYS` | `14` | How long `GeoTrafficSample` rows are kept. |

Also requires (already-existing settings, shared with
[proxy_hosts.md](proxy_hosts.md)'s write path): `TRAEFIK_MANAGER_URL` +
`TRAEFIK_MANAGER_API_KEY`, and an access-log path configured on the
gateway side (Traefik Manager's `ACCESS_LOG_PATH` or Settings UI) — without
either, `tail_access_log` degrades to `None` and the collector simply has
nothing to poll, same convention as every other optional integration here.

## The one read endpoint

`GET /api/traffic/geo?slug=<optional>` (admin-only, same
`require_admin` gate as every other `/api/*` router) — aggregated recent
country/IP counts:

```json
{
  "enabled": true,
  "countries": [
    {"country_code": "US", "country_name": "United States", "bucket": "country", "hits": 42},
    {"country_code": "", "country_name": "", "bucket": "local", "hits": 17}
  ],
  "top_ips": [
    {"client_ip": "8.8.8.8", "bucket": "country", "country_code": "US", "hits": 42}
  ],
  "sample_count": 2
}
```

`?slug=` narrows to one service's proxy hosts; omitted, every proxy host's
samples are summed. `{"enabled": false, ...empty...}` when `GEOIP_ENABLED`
is off. Deliberately bare — no page, no dashboard nav entry (the sibling
nav-redesign PR currently in flight against `dev` touches `base.html`'s
nav; a second page/entry right now would collide with it) — this is the
hook the deferred dashboard follow-up calls.

## Failure handling

Every step degrades, logs, and skips rather than raising or crashing the
poll:

- `tail_access_log` unconfigured/unreachable -> `None`, poll returns 0,
  pruning still runs.
- An unparsable line -> `None` from `parse_line`, logged at debug, skipped.
- GeoIP database missing/unavailable -> every non-local IP resolves as
  `"unknown"`, never a raised exception.
- A router with no matching `ProxyHost` -> that line's hit is simply not
  stored (nothing to attribute it to).

Same fail-open philosophy as every other optional integration in this app.
