# Probing through a gateway

The common homelab shape: the orchestrator is not on the guests' network, and
the guests are not directly exposed — not over HTTP, TCP or SSH. Everything
reaches them through a gateway that also runs the reverse proxy.

That makes the gateway two things at once, and both need modelling:

* a **hop** — HTTP goes via its proxy, SSH via ProxyJump
* a **blocker** — if it is down, every downstream probe fails for a *network*
  reason, which is not the same as a service failure

Getting the second one wrong is the expensive mistake. Without it, a single
gateway outage looks identical to every service breaking at once, and the
distinction between "my network has a problem" and "my data may be corrupt" —
the thing the whole three-state model exists to preserve — is lost.

---

## 1. Probe the gateway directly

The gateway is the one host the orchestrator *can* reach, which is what makes it
usable as a reference point. Probe it without any indirection:

```yaml
  - slug: gateway-vm
    name: Gateway
    probes:
      - name: proxy-listening
        kind: tcp
        required: true
        config:
          host: 10.0.0.2      # the gateway's LOCAL address
          port: 443
```

An HTTP probe against the proxy's own health endpoint works equally well. What
matters is that this probe does not depend on anything else.

## 2. Declare the dependency

```bash
orchestrator-cli service update haos --depends-on gateway-vm
```

```yaml
    depends_on:
      - gateway-vm
```

Now, when the gateway is not HEALTHY:

```
gateway-vm               FAILED
    1 required probe(s) failed: proxy-listening (connection refused)

haos                     UNKNOWN
    blocked: dependency 'gateway-vm' is FAILED (...). This service was not
    probed, so this is NOT evidence about 'haos' itself.
```

Three things to notice:

* downstream is **UNKNOWN, not FAILED** — FAILED marks a service as a restore
  candidate, and inheriting that from the gateway would propose restoring
  services whose data was never inspected;
* the reason **names the gateway**, so one glance tells you where to look;
* the downstream probes **do not run at all**. With 27 services behind one
  gateway that saves 27 timeouts, and avoids 27 error messages describing the
  wrong problem.

Dependencies are scanned once per run and cached, so the gateway is probed once
no matter how many services sit behind it. Cycles are rejected when the file is
loaded, and guarded again at scan time.

## 3. HTTP through the proxy, by local IP

Point the URL at the proxy's **local** address and let `host_header` do the
routing:

```yaml
      - name: http-via-proxy
        kind: http
        required: true
        config:
          url: "https://10.0.0.2/health"     # the proxy's LOCAL ip
          host_header: media.example.com     # what the proxy routes on
          expect_status: [200]
```

This is exactly `curl --resolve`. TLS SNI defaults to `host_header`, so a real
certificate still validates — without that, TLS would be negotiated against a
bare IP and fail. Override with `sni_hostname` in the rare case they differ.

**Why not just use the public DNS name?** Because that resolves outward, so the
probe would also be testing hairpin NAT, external DNS, and the public
certificate chain. Those are worth monitoring — but not from a probe whose job
is to answer "is this service healthy enough to back up?". A public-DNS failure
should not block a backup of a perfectly healthy service.

Works with any proxy — NPM, Traefik, Caddy — since it relies only on
`Host`-header routing.

## 4. SSH through the gateway

Shell probes hop via ProxyJump:

```yaml
      - name: docker
        kind: docker_project
        config:
          project: media-stack
          transport:
            type: ssh
            host: 10.0.0.10       # the guest, on the far side
            user: root
            jump_host: 10.0.0.2   # the gateway
```

Set a default for every probe with `SSH_JUMP_HOST` in `.env`, and override
per-probe. `jump_host: ""` means an explicit direct connection, for a host that
*is* routable.

The jump connection is opened as a distinct step so the failure modes stay
distinguishable:

```
ssh: could not reach the JUMP HOST root@10.0.0.2:22 (...). The target
10.0.0.10 was never contacted, so this says nothing about it.
```

versus a failure reaching the guest *through* a working gateway. Same reason as
everywhere else in this project: knowing *which* thing is broken is most of the
value.

---

## Putting it together

```
gateway-vm        tcp 10.0.0.2:443              (no dependencies)
  └── haos        http 10.0.0.2 + Host header   depends_on: [gateway-vm]
  └── media       http 10.0.0.2 + Host header   depends_on: [gateway-vm]
      └── ssh via jump_host 10.0.0.2
```

`config/services.example.yaml` carries a worked version of this.

Check what the gate will decide before trusting a schedule:

```bash
orchestrator-cli scan --all
```
