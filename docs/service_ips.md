# Service IPs

Every service can be linked to an IP address. Two things use it:

* **Proxy hosts** — the add/edit form shows the linked service's IP next to
  the service picker and offers **Use service IP**, which fills the upstream
  with `http://<ip>:` and leaves the cursor ready for the port. It never
  overwrites a typed upstream without that click.
* **Ping probes** — `kind: ping` with a blank `host` pings the service's IP,
  looked up each time the probe runs, so a re-discovered address is followed
  without editing the probe.

## Effective IP: manual first, then discovered

| | Where it lives | Who sets it |
|---|---|---|
| **Manual** `ip_address` | `services.yaml` (and the `service` table) | you: service page, `orchestrator-cli service update SLUG --ip 10.0.0.5`, or the YAML |
| **Discovered** | the database only (`service_state`) | the IP discovery job / *Discover now* |

The **effective IP** is the manual one when set, otherwise the discovered one.
Discovery never writes the manual field and never touches `services.yaml`, so
it can't make the registry "dirty" — Save to YAML only ever writes what you
typed. Clear the manual IP (`--ip ''`, or empty the field) to go back to the
discovered address.

The manual IP is a plain IPv4 or IPv6 address (no `/prefix`, port or
hostname). The same validator runs for the YAML, the API and the CLI.

When the effective IP changes — discovery found a new address, or you set or
cleared the manual one — an audit entry `service.ip_changed` (`old`, `new`,
`source`) is written.

## Where a discovered IP comes from

Sources are tried in this order; the first one that answers wins, and the
service page shows which (and when):

1. **Proxmox** — needs no extra setup.
   * **VM:** `GET /nodes/{node}/qemu/{vmid}/config` gives the NIC MACs (every
     `netN`, whatever the model: `virtio=`, `e1000=`, `rtl8139=`,
     `vmxnet3=` …). If the VM has the agent enabled (`agent: 1`),
     `GET /nodes/{node}/qemu/{vmid}/agent/network-get-interfaces` gives its
     addresses. Loopback and link-local addresses are skipped, and the
     address on the NIC whose MAC matches the config wins (so `docker0` or a
     VPN tunnel inside the guest is not picked). Source: **guest agent**.
     An agent that is not installed or not running is a normal miss.
   * **Container:** the config's `netN` gives `hwaddr=`; a static `ip=10.0.0.5/24`
     is itself the answer. With `ip=dhcp`, `GET /nodes/{node}/lxc/{vmid}/interfaces`
     gives the live addresses. Source: **LXC**.
2. **ARP** — the MACs from step 1 looked up in a neighbour table
   (`ip neigh show`, falling back to `arp -an`; FAILED / INCOMPLETE entries are
   ignored). Source: **ARP**.
3. **DHCP** — the MACs looked up in a dnsmasq leases file. Source: **DHCP**.

Only guests Proxmox reports as **running** are asked — the same guest state
the health engine's guest gate uses. A stopped VM's agent cannot answer and
its ARP entry is stale by definition. When nothing answers, the last known
address is kept (an agent that stopped answering does not mean the address
changed) and the reason is shown under it.

Services without a Proxmox guest (`guest_kind: none`) have nothing to ask:
give them a manual IP.

### Proxmox privileges

| Read | Privilege |
|---|---|
| guest config (MACs, LXC `ip=` / `/interfaces`) | `VM.Audit` — already in the routine token |
| QEMU guest agent | `VM.Monitor` (PVE 8) / `VM.GuestAgent.Audit` (PVE 9+) — optional |

A 403 is not fatal: the service page names the missing privilege and
discovery carries on with what it has (no MACs → nothing to look up; MACs but
no agent → ARP / DHCP). See [api_tokens.md](api_tokens.md).

## ARP and DHCP: where they run

The container's own ARP table only sees its Docker bridge, so ARP and DHCP
reads run over the **same transports probes use** — usually the host runner,
targeting the gateway or another host on the guests' LAN
([host_runner.md](host_runner.md), [probing_through_a_gateway.md](probing_through_a_gateway.md)).
One setting holds the transport, in the shape of a probe's `transport:` block,
as JSON:

```bash
IP_DISCOVERY_TRANSPORT='{"type": "host_agent", "host": "gateway"}'
# or {"type": "ssh", "host": "10.0.0.1", "user": "orchestrator"}
# or local   (only useful with network_mode: host)
```

Blank (the default) = Proxmox-only discovery, no ARP / DHCP.

With the host runner the commands are ad-hoc commands, so the runner needs
`allow.command: true` and these in `allow.commands` (keep the list narrow):
`ip` (or `arp`), `cat` for the leases file, and for the sweep `fping` or `sh`
+ `ping`.

| Setting | Default | Meaning |
|---|---|---|
| `IP_DISCOVERY_ENABLED` | `true` | run the periodic job |
| `IP_DISCOVERY_INTERVAL_S` | `600` | how often (minimum 30) |
| `IP_DISCOVERY_TRANSPORT` | blank | where ARP / DHCP reads run (above) |
| `IP_DISCOVERY_ARP_ENABLED` | `true` | read the neighbour table |
| `IP_DISCOVERY_SWEEP_CIDRS` | blank | comma-separated CIDRs to "ARP ping" first |
| `IP_DISCOVERY_DHCP_LEASES_PATH` | blank | dnsmasq leases file on the transport host |

### ARP ping (the sweep)

A guest that has not talked to the transport host lately is not in its ARP
table. `IP_DISCOVERY_SWEEP_CIDRS=192.168.100.0/24` pings that range first —
with `fping -a -q -g <cidr>` when fping is installed, otherwise a bounded
parallel `ping -c1 -W1` loop (64 at a time). Capped at a /22 (1024 addresses)
per CIDR. The sweep runs once per discovery cycle, and only when some
service actually needs the ARP fallback.

### DHCP leases

`IP_DISCOVERY_DHCP_LEASES_PATH=/var/lib/misc/dnsmasq.leases` reads a
dnsmasq-format file (`expiry mac ip hostname clientid`, as also written by Pi-hole)
with `cat` over the transport. Expired leases are ignored. The reader is a
small pluggable `DhcpLeaseSource` (`ipdiscovery/dhcp.py`); OPNsense / pfSense
API and Kea readers are on the roadmap.

The ARP table and the leases are each read **once per cycle** and shared by
every service in it.

## Running it

* **Periodically** — the IP discovery job (lifespan, like the other
  schedulers; a failure to start is logged, not fatal).
* **Service page → Overview → Address → Discover now.**
* **Dashboard** — tick services, **Discover IP** in the bulk bar
  (`POST /api/bulk/services` with `"action": "discover_ip"`).
* **API** — `POST /api/services/{slug}/ip/discover`, `GET /api/services/{slug}/ip`.
* **CLI** — `orchestrator-cli ip discover SLUG...` / `--all`, and
  `orchestrator-cli ip show` for every service's IP, source and MACs.

## Ping probe

```yaml
probes:
  - name: ping
    kind: ping
    required: false
    config:
      host: ""          # blank = this service's IP
      count: 1          # 1..10
      transport:        # optional — default: from the container itself
        type: host_agent
        host: gateway
```

It runs `ping -c <count> -W <wait> <host>` over the transport:

| Outcome | Verdict |
|---|---|
| exit 0 | HEALTHY, latency = average RTT |
| exit 1 (no reply) | FAILED |
| ping could not run, exit 2, or the service has no IP yet | UNKNOWN |

The service page's **Add ping probe** (shown when the service has an IP and no
ping probe yet) creates an optional (`required: false`) ping probe with a
blank host, using `IP_DISCOVERY_TRANSPORT` as its transport when that is set
— so it pings from where ARP is read. A ping says the *host* is up, not that
the service is healthy; pair it with a real check before making it required.

ICMP needs the `NET_RAW` capability (Docker grants it by default; if you drop
all capabilities, add it back with `cap_add: [NET_RAW]`) or a
`net.ipv4.ping_group_range` that includes the container's group. The image
ships `iputils-ping` and `iproute2` for local pings and `ip neigh`.
