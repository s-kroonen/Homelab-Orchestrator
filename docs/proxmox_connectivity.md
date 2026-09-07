# Proxmox connectivity: single entry point, quorum, and failover

The orchestrator talks to Proxmox through **one** `PROXMOX_HOST`. On a normal
cluster that is fine — Proxmox proxies API requests between nodes, so a call to
`/nodes/burst-a/vzdump` sent to *any* node is forwarded to `burst-a`. You do not
need one endpoint per node to *address* every node.

What that single endpoint does create is a single point of failure, and on a
cluster where most nodes are deliberately powered off it creates a second, less
obvious problem: **quorum**.

---

## 1. Today: point at the always-on node

**`PROXMOX_HOST` must be the always-on node.**

This is not a preference. If it points at a burst node, then whenever that node
is powered off the orchestrator cannot reach the Proxmox API *at all* — which
includes the API calls it would use to bring things back. The control plane
would be locked out by exactly the condition it exists to resolve.

The always-on node is reachable by definition, and it can address the other two
on the orchestrator's behalf once they are up.

---

## 2. The quorum problem — check this before phase 3

A Proxmox cluster requires **quorum** (a majority of votes) before it will allow
write operations: starting a guest, running `vzdump`, changing config. Reads
keep working without it.

On a 3-node cluster each node has one vote, so quorum needs **2 of 3 online**.

That is a direct conflict with the design goal here: *two of the three nodes are
normally off*. With only the always-on node running, the cluster has 1 of 3
votes, loses quorum, and refuses to start guests — so the wake pipeline in
phase 3 would fail at the last step even though every other piece worked.

If you have already solved this, nothing to do. If not, the options are:

| Option | How it works | Trade-off |
|--------|--------------|-----------|
| **QDevice** (recommended) | Run `corosync-qnetd` on the always-on Pi. It casts a tiebreaker vote, so always-on + QDevice = 2 votes = quorum with a single node up. | One extra package on the Pi; the Pi becomes quorum-relevant. This is the intended Proxmox answer for exactly this shape of cluster. |
| **Lower expected votes** | `pvecm expected 1` lets a single node operate alone. | Not persistent across reboots, and it removes split-brain protection. Fine as a manual break-glass, wrong as a standing configuration. |
| **Don't cluster** | Run the three nodes standalone, each with its own API endpoint. | No quorum concerns at all, and burst nodes genuinely independent. Costs you the single-pane cluster view and cross-node migration. The orchestrator would then need the multi-endpoint work in §3 as a *requirement*, not an enhancement. |

Worth confirming with `pvecm status` on the always-on node while the burst nodes
are down. If it reports `Quorate: No`, this needs resolving before the wake
pipeline can start guests.

---

## 3. Later phase: multi-endpoint failover

Once the above is settled, redundant control is a contained change. The design:

### Config shape

The `nodes:` list in `services.yaml` already enumerates the cluster. Give each
node an optional API endpoint:

```yaml
nodes:
  - name: always-on-gateway
    always_on: true
    api_host: gateway.example.lan      # new, optional
    api_port: 8006                     # new, optional, defaults to 8006

  - name: compute-a
    always_on: false
    api_host: compute-a.example.lan
```

`PROXMOX_HOST` stays as the default/bootstrap endpoint for when the registry has
not loaded yet.

### Adapter shape

A `MultiEndpointProxmoxAdapter` wrapping N `ProxmoxFamilyClient`s:

1. **Preference order**: always-on nodes first, then the rest. An always-on node
   is the most likely to answer.
2. **Sticky**: cache the last endpoint that worked and keep using it; do not
   round-robin. Reconnecting per call wastes time and muddies logs.
3. **Fail over only on `AdapterUnreachable`.** This is the important rule, and
   the existing error hierarchy already encodes it:
   * `AdapterUnreachable` — that node is down or unroutable. Try the next one.
   * `AdapterAuthError` — the token is wrong. It will be wrong on *every* node,
     because PVE tokens are cluster-wide. Retrying elsewhere just multiplies the
     failed auth attempts and hides the real cause. **Fail immediately.**
   * `AdapterRequestError` — the cluster answered and said no. A different
     endpoint would give the same answer. **Fail immediately.**
4. **Surface which endpoint served the request** in the pipeline step log, so a
   silent failover is still visible after the fact.

### What it does not solve

Failover buys you *reachability*, not *capability*. If the cluster lacks quorum,
every endpoint returns the same refusal — reaching a second node does not help.
That is why §2 comes first: multi-endpoint failover on a non-quorate cluster is
redundancy that cannot actually do anything.

### Scope estimate

Roughly: two optional columns on `node` (one migration), a wrapper adapter of
~80 lines, and endpoint attribution in the step recorder. Small — the value of
deferring it is only that §2 might change the answer (standalone nodes would
make it mandatory and reshape the config).
