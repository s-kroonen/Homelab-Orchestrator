# Controllers and actions

A **controller** is how the orchestrator acts on ONE service itself — restart a
container, an add-on, a unit — without touching the guest it lives on. Set it in the
service's `controller:` block (the **Controller (JSON)** box on the service page, or
`services.yaml`).

| `type` | Acts on | Needs |
|---|---|---|
| `portainer` | a container or stack | `PORTAINER_URL`, `PORTAINER_API_KEY` (an access token). `{type: portainer, endpoint: 2, container: nginx}` or `stack: media` |
| `haos` | a Home Assistant add-on or the Core | `HAOS_URL`, `HAOS_TOKEN` (long-lived token of an **admin** user). `{type: haos, addon: core_mosquitto}` or `{type: haos, core: true}` |
| `docker` | a container via the docker CLI | a transport. `{type: docker, container: nginx, transport: {type: host_agent, host: media-vm}}` |
| `systemd` | a unit | a transport. `{type: systemd, unit: caddy, transport: {...}}` |
| `command` | anything | a transport and argv lists: `{type: command, restart: ["/opt/restart.sh"], status: [...], transport: {...}}` |

Transports are the same blocks probes use (ssh, the host runner); commands are argv
lists, never shell strings. Under `DRY_RUN=true` the Portainer and Home Assistant
controllers only log, and command controllers use the recording transport.

A **root service with no controller** acts on its Proxmox guest (start / stop /
restart the VM or container). A **sub-service with none** can't be acted on by itself.
`target: guest` always means the root's guest.

## Where actions are

* Service page → **Actions**: live status of the service and its guest, Start / Stop /
  Restart for each.
* Dashboard → tick services → **Start / Stop / Restart (itself)**.
* API: `GET /api/services/{slug}/controller?live=true`,
  `POST /api/services/{slug}/actions/{start|stop|restart}` with `{"target": "service"|"guest"}`.

Every action is audited (`service.start|stop|restart`). Stop and restart are refused
while a backup of the root is running. Nodes keep their own power actions (Wake /
Power off / Restart on the dashboard).
