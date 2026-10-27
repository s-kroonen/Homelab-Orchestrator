# Two sites, two orchestrators (design notes)

**Status: design only.** Connections carry the groundwork (see [connections.md](connections.md));
nothing below runs yet.

## The goal

Run a second orchestrator at the offsite location from the same image. Each one is responsible
for its own side and keeps working when the other is unreachable. The two know about each other
so that **status checks and notifications are shared**, and so that one can take over the other's
notifications and recovery when it goes dark: an orchestrator that lives on the thing that failed
cannot tell you it failed.

## What exists today

* **`site`** on every connection and `ORCHESTRATOR_SITE` for the orchestrator itself: where
  something lives.
* **`scope`**: `local` (reachable only from this LAN: its Proxmox, its PBS) or `shared` (the
  same on both: an offsite Portainer, an ntfy server, a webhook).
* **Stable ids**: a connection is addressed by id everywhere (controllers, probes, the API), so the
  same id means the same thing on both orchestrators.
* **Export / import** of the shared set without secrets (`connections export` / `import`).

## The shape it is heading for

1. **Independent.** No leader, no shared database. Each orchestrator has its own registry, its own
   DB, its own `connections.yaml` (local connections differ, shared ones are the same).
2. **A peer is a connection.** Another orchestrator is added as a connection of kind `peer`
   (URL + a token scoped to read status and post notices). Its `site` names the side it owns.
3. **Status exchange.** Every orchestrator publishes a small signed summary of its own checks
   (verdicts per service and node, last heartbeat) and polls its peers'. The dashboard shows both
   sides, labelled by site.
4. **Heartbeat and takeover.** A peer that stops answering past a threshold (with the usual
   confirm-scans, so one blip is not an outage) is reported by the other side: "offsite
   orchestrator unreachable". Notifications that depend on a site's own network (its Proxmox is
   down, its power manager) are raised by the peer from the last summary it received, and marked
   as coming from the standby.
5. **Shared notifications.** Channels with scope `shared` are used by both; each side de-duplicates
   by the event's `(site, service, kind)` key so one problem is one message, and a takeover
   message says so.
6. **Recovery stays local.** Wake, restore and deploy act through local connections only; a peer
   never operates the other site's infrastructure. Takeover is about *telling you*, not about
   running the other side's recovery with credentials it should not have.

## Open questions

* How the peer token is issued and rotated (an MCP-style scoped token is the likely answer).
* Clock skew and the heartbeat threshold; how the two settle who raises a shared alert.
* Whether to share backup history and service state read-only, or only verdicts.
* Splitting a service registry across sites: each orchestrator's `services.yaml` stays its own, a
  service has one home site.

None of this changes how you use connections today; it is why `site`, `scope` and stable ids are
already in the file.
