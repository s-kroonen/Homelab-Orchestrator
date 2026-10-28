/* The dashboard (templates/dashboard.html), to the Main and PhoneDashboard wireframes: what
 * needs attention first, the numbers, the nodes, recent activity, every service at a glance.
 * Data: GET /api/views/dashboard (the database, at once); then what needs a live read: node
 * power, the proxy hosts' state in Traefik, the connections. Refreshed in place, never reloaded. */
(function () {
  "use strict";
  const { $, el, clear, fill, api, fmt, region, confirm, menuButton } = window.UI;
  const O = window.Objects;
  const toast = (m, k) => window.toast(m, k);
  const enc = encodeURIComponent;
  let V = JSON.parse($("#initial").textContent);
  let live = null;      // /api/infra/nodes/status
  let changed = [];     // proxy hosts changed in Traefik (live)
  let conns = null;     // /api/infra/overview

  // --- needs attention ----------------------------------------------------------------------
  async function check(slug) { try { await O.bulk("services", "resync", [slug]); } catch (_e) { /* toast */ } view.refresh(); }
  async function putBack(h) {
    try {
      const r = await api.post("/api/push", { hostnames: [h.hostname] });
      const bad = (r.results || []).filter((x) => !x.ok);
      toast(bad.length ? bad.map((x) => x.message).join("; ") : "Put back " + h.hostname, bad.length ? "error" : "ok");
    } catch (_e) { /* toast */ }
    loadHosts();
  }
  function items() {
    const list = V.attention.slice();
    for (const h of changed) list.push({ kind: "changed", cls: "warn", label: "Changed", slug: h.service_slug, title: (h.service_slug || h.hostname) + " was changed outside the orchestrator",
      text: "Proxy host " + h.hostname + ": " + ((h.sync_changes || []).join(", ") || "differs in Traefik Manager"), host: h });
    return list;
  }
  function action(it) {
    if (it.kind === "failed") return el("button", { type: "button", class: "secondary small", onclick: () => check(it.slug) }, "Check now");
    if (it.kind === "backup") return el("a", { class: "btn secondary small", href: "/activity?kind=backup&service=" + enc(it.slug) }, "Open run");
    if (it.kind === "changed") return el("button", { type: "button", class: "secondary small", onclick: () => putBack(it.host) }, "Put it back");
    return null;
  }
  function drawAttention() {
    const list = items();
    const fails = list.filter((i) => i.cls === "fail").length;
    const warns = list.length - fails;
    fill($("#attn-counts"), fails ? el("span", { class: "pill fail" }, fails + " failed") : null, " ", warns ? el("span", { class: "pill warn" }, warns + " warning" + (warns === 1 ? "" : "s")) : null);
    $("#summary").textContent = list.length
      ? list.length + (list.length === 1 ? " thing needs" : " things need") + " attention. Everything else that is switched on is fine."
      : "Everything that is switched on is fine.";
    const box = clear($("#attention"));
    if (!list.length) { box.append(el("div", { class: "dt-empty" }, el("strong", {}, "Nothing needs attention"), el("div", {}, "Every service that is switched on answers and its last backup went through."))); return; }
    for (const it of list) {
      box.append(el("div", { class: "attn-row" },
        el("span", { class: "pill " + it.cls }, it.label),
        el("div", { style: { minWidth: 0 } }, el("strong", {}, it.title), el("div", { class: "faint", style: { fontSize: "13px", overflowWrap: "anywhere" } }, it.text || "")),
        el("span", { class: "faint", style: { fontSize: "12px", whiteSpace: "nowrap" } }, it.at ? fmt.ago(it.at).replace(" ago", "") : ""),
        action(it) || el("span"),
        it.slug ? el("a", { class: "btn ghost small", href: "/services/" + enc(it.slug) }, "Open") : el("span")));
    }
  }

  // --- tiles ------------------------------------------------------------------------------------
  function drawTiles() {
    const by = V.services.by_state;
    const parts = (pairs) => pairs.filter(([n]) => n).map(([n, w]) => n + " " + w).join(" · ");
    const nodeStates = live ? live.nodes.map((n) => n.state) : [];
    const on = nodeStates.filter((s) => s === "on").length;
    const tile = (label, value, sub, href) => el(href ? "a" : "div", { class: "stat-tile", href: href || null, style: href ? { color: "inherit", textDecoration: "none" } : null },
      el("div", { class: "stat-label" }, label), el("div", { class: "stat-value" }, value), el("div", { class: "stat-sub" }, sub || ""));
    const up = conns ? conns.connections.filter((c) => c.state === "up").length : null;
    const counted = conns ? conns.connections.filter((c) => c.state === "up" || c.state === "down").length : null;
    fill($("#tiles"),
      tile("Services", String(V.services.total), parts([[by.ok, "healthy"], [by.fail, "failed"], [by.warn, "warning"], [by.off, "off"]]) || "none yet", "/services"),
      tile("Nodes", String(V.nodes.length), live ? (on === V.nodes.length ? (V.nodes.length === 2 ? "both on" : "all on") : on + " on, " + (V.nodes.length - on) + " off or unknown") : "checking…", "/nodes"),
      tile("Proxy hosts", String(V.proxy_hosts), changed.length ? changed.length + " changed in Traefik" : "", "/proxy-hosts"),
      tile("Backups last 24 h", V.backups.runs ? V.backups.ok + "/" + V.backups.runs : "—", V.backups.next_run ? "next " + fmt.time(V.backups.next_run).split(" ").pop().slice(0, 5) + " · " + V.backups.next_policy : "nothing scheduled", "/backup-policies"),
      tile("Connections", conns ? (counted ? up + "/" + counted : "—") : "…", conns ? (!counted ? "none checked" : up === counted ? "all reachable" : (counted - up) + " not answering") : "checking…", "/infra/connections"));
  }

  // --- nodes -----------------------------------------------------------------------------------------
  function drawNodes() {
    const box = clear($("#nodes"));
    if (!V.nodes.length) { box.append(el("p", { class: "muted", style: { padding: "0 18px 12px", margin: 0 } }, "No nodes yet. ", el("a", { href: "/sources#proxmox" }, "Import them from Proxmox"), ".")); return; }
    for (const n of V.nodes) {
      const l = live && live.nodes.find((x) => x.name === n.name);
      const state = !l ? (live ? "unknown" : "…") : l.state || "unknown";
      const cls = state === "on" ? "ok" : state === "off" || state === "unknown" || state === "…" ? "off" : "pending";
      const canWake = !n.always_on && !!n.power_mgr_target;
      const h = l && l.health;
      const bits = [n.always_on ? "always on" : canWake ? "wakes on demand" : "no power target", n.services + " service" + (n.services === 1 ? "" : "s"),
        h && h.reachable ? [h.version, h.latency_ms != null ? Math.round(h.latency_ms) + " ms" : ""].filter(Boolean).join(" · ") : ""].filter(Boolean).join(" · ");
      const refresh = () => { nodeStatus.refresh(); view.refresh(); };
      box.append(el("div", { class: "attn-row", style: { gridTemplateColumns: "auto minmax(0,1fr) auto 32px" } },
        el("span", { class: "pill " + cls }, state),
        el("div", { style: { minWidth: 0 } }, el("strong", { class: "mono" }, n.name), el("div", { class: "faint", style: { fontSize: "12.5px" } }, bits)),
        state === "off" && canWake
          ? el("button", { type: "button", class: "secondary small", onclick: () => O.nodePower(n, "wake", refresh) }, "Wake")
          : el("button", { type: "button", class: "secondary small", onclick: () => nodeStatus.refresh() }, "Check"),
        menuButton(() => [
          { label: "Open on Nodes & guests", href: "/nodes" },
          canWake ? { label: "Wake", onClick: () => O.nodePower(n, "wake", refresh) } : null,
          n.power_mgr_target ? { label: "Restart…", onClick: () => O.nodePower(n, "restart", refresh) } : null,
          n.power_mgr_target ? { label: "Power off…", onClick: () => O.nodePower(n, "power_off", refresh) } : null,
        ], "Actions for " + n.name)));
    }
  }

  // --- glance ------------------------------------------------------------------------------------------
  function drawGlance() {
    const box = clear($("#glance"));
    if (!V.glance.length) { box.append(el("div", { class: "card dt-empty" }, el("strong", {}, "No services yet"), el("div", {}, "Import your Proxmox guests or add a service."), el("div", { style: { marginTop: "10px" } }, el("button", { type: "button", class: "small", onclick: newService }, "New service")))); return; }
    for (const g of V.glance) {
      box.append(el("div", { class: "card", style: { margin: 0 } }, el("div", { class: "faint", style: { fontSize: "12.5px", marginBottom: "8px" } }, g.name),
        el("div", { class: "tags", style: { gap: "6px" } }, g.services.map((s) => el("a", { class: "pill " + s.cls, href: "/services/" + enc(s.slug), title: s.name + ": " + s.label, style: { textDecoration: "none" } }, s.slug)))));
    }
  }

  // --- sections ---------------------------------------------------------------------------------------
  function drawAll() { drawAttention(); drawTiles(); drawNodes(); drawGlance(); }
  const view = region(el("div"), {
    initial: V,
    load: () => api.get("/api/views/dashboard", { suppressToast: true }),
    render: (d) => { V = d; drawAll(); },
    topics: ["health", "run", "registry"], wait: 1500, age: $("#attn-age"),
  });
  const nodeStatus = region(el("div"), {
    load: () => api.get("/api/infra/nodes/status", { suppressToast: true }),
    render: (d) => { live = d; drawNodes(); drawTiles(); },
    topics: ["run"], wait: 3000, every: 60000, poll: true,
  });
  function loadHosts() {
    api.get("/api/proxy-hosts", { suppressToast: true }).then((hosts) => { changed = hosts.filter((h) => h.enabled && h.sync_state === "detached"); drawAttention(); drawTiles(); }, () => {});
  }
  if (window.OrchLive) window.OrchLive.on(["registry", "pending"], loadHosts, { wait: 3000 });
  const recent = region($("#recent"), {
    load: () => api.get("/api/activity?limit=6", { suppressToast: true }),
    render: (list, box) => { clear(box); if (!list.length) box.append(el("p", { class: "muted" }, "Nothing yet.")); list.forEach((ev) => box.append(O.eventRow(ev, { service: true }))); },
    topics: ["run", "audit", "alert"], wait: 1500, skeletonRows: 4,
  });
  function newService() { O.serviceDrawer({ view: V.view, onSaved: () => view.refresh() }); }
  $("#new-service").addEventListener("click", newService);
  $("#check-all").addEventListener("click", async () => {
    if (!await confirm({ title: "Check everything now?", body: "Every enabled service is checked now (its guest first, then its probes) instead of waiting for the next scan. This can take a minute.", action: "Check everything", danger: false })) return;
    toast("Checking every service…");
    try { const r = await api.post("/api/infra/resync", {}, { suppressToast: true }); toast("Checked " + (r.results || []).length + " services", "ok"); } catch (e) { toast(e.message, "error"); }
    view.refresh(); nodeStatus.refresh();
  });
  loadHosts();
  api.get("/api/infra/overview", { suppressToast: true }).then((d) => { conns = d; drawTiles(); }, () => {});
  void recent;
})();
