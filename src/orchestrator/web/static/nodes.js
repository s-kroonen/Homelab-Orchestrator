/* Nodes & guests (templates/nodes.html).
 * The node cards paint at once from the database (/api/views/nodes); their live state (power,
 * Proxmox) comes from /api/infra/nodes/status and the guests from /api/infra/proxmox/guests,
 * each in its own section so a node that does not answer holds nothing else back. */
(function () {
  "use strict";
  const { $, el, clear, api, fmt, region, table, menuButton, confirm, drawer } = window.UI;
  const { nodeDrawer, nodePower, deleteNode, serviceDrawer, bulk } = window.Objects;
  let view = JSON.parse($("#initial").textContent);
  let svcView = JSON.parse($("#services-initial").textContent);
  let live = null;   // /api/infra/nodes/status
  let guests = null; // /api/infra/proxmox/guests

  // --- node cards ---------------------------------------------------------------------------
  function upFor(s) {
    if (!s) return "";
    const d = Math.floor(s / 86400); const h = Math.floor((s % 86400) / 3600);
    return d ? d + " d" : h + " h";
  }
  function liveOf(name) { return live && live.nodes.find((n) => n.name === name); }
  function stateOf(node) {
    const l = liveOf(node.name);
    if (!l) return { cls: "off", label: live ? "unknown" : "…", why: live ? "" : "checking" };
    if (l.state === "on") return { cls: "ok", label: "on", why: l.power_detail || "" };
    if (l.state === "off") return { cls: "off", label: "off", why: l.power_detail || "" };
    const why = (l.health && l.health.error) || l.power_detail || "";
    if (!l.state || l.state === "unknown") return { cls: "off", label: "unknown", why };
    return { cls: "pending", label: l.state, why };
  }
  function proxmoxText(node) {
    const l = liveOf(node.name);
    if (!l) return live ? "unknown" : "checking…";
    const h = l.health || {};
    if (h.reachable) return [h.version, h.uptime_s ? "up " + upFor(h.uptime_s) : "", h.latency_ms != null ? Math.round(h.latency_ms) + " ms" : ""].filter(Boolean).join(" · ");
    if (!h.checked) return h.error || "not checked";
    return "not answering" + (l.state === "off" ? " (off)" : "");
  }
  const fmtSecs = fmt.secs;
  function bootText(node) {
    const b = node.boot || {};
    if (!b.read_at) return el("span", { class: "faint" }, "not read yet: it is read while the node is on");
    const n = b.sequence.length;
    return [n ? n + " guest" + (n === 1 ? "" : "s") + " start by themselves" : "no guest starts by itself",
      b.delay_s ? " · after " + fmtSecs(b.delay_s) : ""].join("");
  }
  function guestText(node) {
    if (!guests) return "…";
    const mine = guests.filter((g) => g.node === node.name);
    return mine.length + " (" + mine.filter((g) => g.status === "running").length + " running)";
  }
  const fact = (label, value, mono) => el("div", {}, el("div", { class: "faint", style: { fontSize: "12px" } }, label), el("div", { class: mono ? "mono" : null }, value == null || value === "" ? "—" : value));

  function drawCards() {
    const box = $("#node-cards");
    clear(box);
    if (!view.nodes.length) {
      box.append(el("div", { class: "card dt-empty" }, el("strong", {}, "No nodes yet"),
        el("div", {}, "Import them from Proxmox: every node and guest of the cluster, in one go."),
        el("div", { style: { marginTop: "10px" } }, el("a", { class: "btn small", href: "/sources#proxmox" }, "Import from Proxmox"))));
      return;
    }
    for (const n of view.nodes) {
      const st = stateOf(n);
      const canWake = !n.always_on && !!n.power_mgr_target;
      const refresh = () => { status.refresh(); nodesRegion.refresh(); };
      const primary = st.label === "off" && canWake
        ? el("button", { type: "button", onclick: () => nodePower(n, "wake", refresh) }, "Wake")
        : el("button", { type: "button", class: "secondary", onclick: () => { status.refresh(); guestsRegion.refresh(); } }, "Check");
      box.append(el("article", { class: "card node-card", "aria-label": n.name },
        el("div", { class: "card-head" },
          el("h2", { class: "mono", style: { fontSize: "17px", fontFamily: "var(--mono)" } }, n.name),
          el("span", { class: "pill " + st.cls, title: st.why || null }, st.label),
          el("span", { class: "faint", style: { fontSize: "12.5px" } }, n.always_on ? "always on" : canWake ? "wakes on demand" : "no power target"),
          el("span", { class: "node-acts" }, primary, menuButton(() => [
            { label: "Edit…", onClick: () => nodeDrawer({ node: n, onSaved: refresh }) },
            { label: "Check now", onClick: () => { status.refresh(); guestsRegion.refresh(); } },
            canWake ? { label: "Wake", onClick: () => nodePower(n, "wake", refresh) } : null,
            n.power_mgr_target ? { label: "Restart…", onClick: () => nodePower(n, "restart", refresh) } : null,
            n.power_mgr_target ? { label: "Power off…", onClick: () => nodePower(n, "power_off", refresh) } : null,
            { label: n.wake_on_hit_enabled ? "Turn wake on hit off" : "Turn wake on hit on", onClick: () => bulk("nodes", n.wake_on_hit_enabled ? "wake_on_hit_off" : "wake_on_hit_on", [n.name]).then(refresh, () => {}) },
            "-",
            { label: "Delete…", danger: true, onClick: () => deleteNode(n, refresh).catch(() => {}) },
          ], "Actions for " + n.name))),
        el("div", { class: "node-facts" },
          fact("Address", n.address, true),
          fact("Proxmox", proxmoxText(n)),
          fact("Power target", n.always_on ? "always on" : n.power_mgr_target, !n.always_on),
          fact("Guests", guestText(n)),
          fact("Services", el("a", { href: "/services?group=node&q=" + encodeURIComponent(n.name) }, String(n.services))),
          fact("Wake on hit", n.always_on ? "not needed (always on)" : !n.power_mgr_target ? "no power target" : n.wake_on_hit_enabled ? "yes" : "no"),
          fact("When it boots", bootText(n)),
          n.always_on ? null : fact("Boot takes", "about " + fmtSecs(n.boot.boot_s) + (n.boot.boot_learned ? " (its last wakes)" : " (a guess until it has been woken)"))),
        n.notes ? el("p", { class: "faint", style: { margin: "12px 0 0", fontSize: "13px" } }, n.notes) : null));
    }
  }

  // --- guests -------------------------------------------------------------------------------
  const guestAge = el("span", { class: "age" });
  const serviceBySlug = () => new Map(svcView.rows.map((r) => [r.slug, r]));
  function makeService(g) {
    const node = svcView.nodes.find((n) => n.name === g.node);
    serviceDrawer({
      view: svcView,
      prefill: { name: g.name, node_id: node ? node.id : null, guest_kind: g.kind === "lxc" ? "lxc" : "vm", guest_id: g.vmid },
      onSaved: () => { servicesRegion.refresh(); guestsRegion.refresh(); nodesRegion.refresh(); },
    });
  }
  const nth = fmt.nth;
  function bootDrawer(g) {
    const b = g.boot || { onboot: false, order: null, up_s: null, down_s: null };
    const num = (v) => (v === "" || v == null ? null : Number(v));
    drawer({
      title: "Boot settings of " + g.name,
      sub: (g.kind === "lxc" ? "CT " : "VM ") + g.vmid + " on " + g.node + ". Written to Proxmox (its Options → Start at boot and Start/Shutdown order); the node must be on.",
      values: { onboot: !!b.onboot, order: b.order == null ? "" : String(b.order), up_s: b.up_s == null ? "" : String(b.up_s), down_s: b.down_s == null ? "" : String(b.down_s) },
      saveLabel: "Save in Proxmox",
      fields: [
        { key: "onboot", label: "Start at boot: Proxmox starts it when " + g.node + " boots", type: "checkbox",
          help: "Then a wake or a backup that powers " + g.node + " on waits for Proxmox to start it, instead of starting it. Without it, it is started when needed." },
        { key: "order", label: "Order", type: "number", placeholder: "none", show: (v) => v.onboot, help: "Lower starts first. Guests without an order start after the ordered ones." },
        { key: "up_s", label: "Wait after starting it (s)", type: "number", placeholder: "0", show: (v) => v.onboot, help: "Proxmox waits this long before it starts the next guest." },
        { key: "down_s", label: "Shutdown timeout (s)", type: "number", placeholder: "Proxmox default", help: "How long Proxmox waits for it to shut down." },
      ],
      async onSave(v) {
        await api.put("/api/infra/guests/" + encodeURIComponent(g.node) + "/" + g.vmid + "/boot",
          { onboot: !!v.onboot, order: v.onboot ? num(v.order) : null, up_s: v.onboot ? num(v.up_s) : null, down_s: num(v.down_s) }, { suppressToast: true });
        window.toast("Saved " + g.name + "'s boot settings in Proxmox", "ok");
        guestsRegion.refresh(); nodesRegion.refresh();
      },
    });
  }
  async function power(g, action) {
    if (action === "stop") {
      const svc = g.service_slug && serviceBySlug().get(g.service_slug);
      const ok = await confirm({
        title: "Stop " + g.kind.toUpperCase() + " " + g.vmid + " (" + g.name + ")?",
        body: svc ? svc.name + " runs on it and stops with it. It is shut down cleanly." : "It is shut down cleanly.",
        action: "Stop",
      });
      if (!ok) return;
    }
    try {
      await api.post("/api/infra/guests/" + encodeURIComponent(g.node) + "/" + g.vmid + "/power", { action });
      window.toast((action === "start" ? "Starting " : "Stopping ") + g.name, "ok");
    } catch (_e) { return; }
    setTimeout(() => guestsRegion.refresh(), 2500);
  }
  const t = table($("#guests"), {
    rows: [],
    key: (g) => g.node + "/" + g.vmid,
    name: (g) => g.name,
    noun: ["guest", "guests"],
    placeholder: "Filter by name or VMID",
    search: (g) => [g.vmid, g.name, g.node, g.kind, g.service_slug || ""].join(" "),
    sort: { key: "vmid", dir: 1 },
    age: guestAge,
    columns: [
      { key: "vmid", label: "VMID", sort: (g) => g.vmid, render: (g) => el("span", { class: "mono" }, String(g.vmid)) },
      { key: "name", label: "Name", sort: (g) => g.name, render: (g) => el("strong", {}, g.name) },
      { key: "kind", label: "Kind", sort: true, render: (g) => (g.kind === "ct" || g.kind === "lxc" ? "LXC" : g.kind === "vm" ? "VM" : g.kind) },
      { key: "node", label: "Node", sort: true },
      { key: "status", label: "Status", sort: true, phone: false, render: (g) => el("span", { class: "pill " + (g.status === "running" ? "ok" : "off") }, g.status) },
      { key: "boot", label: "Starts with node", sort: (g) => (g.boot && g.boot.onboot ? g.boot.position || 99 : 999), phone: "Starts with node",
        render: (g) => !g.boot ? el("span", { class: "faint" }, "not read yet")
          : !g.boot.onboot ? el("span", { class: "faint" }, "no: started when needed")
          : [el("span", {}, "yes, " + nth(g.boot.position || 0)), el("span", { class: "sub" }, [g.boot.order != null ? "order " + g.boot.order : "no order", g.boot.up_s ? "waits " + g.boot.up_s + " s" : "", g.boot.starts_after_s != null ? "starts ~" + fmtSecs(g.boot.starts_after_s) + " after boot" : ""].filter(Boolean).join(" · "))] },
      { key: "service", label: "Service", sort: (g) => g.service_slug || "~",
        render: (g) => g.service_slug ? el("a", { href: "/services/" + encodeURIComponent(g.service_slug) }, g.service_slug)
          : el("button", { type: "button", class: "ghost small", style: { color: "var(--link)", padding: "0" }, onclick: () => makeService(g) }, "Make a service…") },
      { key: "act", label: "", phone: false, cls: "actions", render: (g) => el("button", { type: "button", class: "secondary small", onclick: () => power(g, g.status === "running" ? "stop" : "start") }, g.status === "running" ? "Stop" : "Start") },
    ],
    chips: [
      { key: "running", label: "Running", test: (g) => g.status === "running" },
      { key: "stopped", label: "Stopped", test: (g) => g.status !== "running" },
      { key: "unlinked", label: "Not a service", test: (g) => !g.service_slug },
    ],
    menu: (g) => [
      g.service_slug ? { label: "Open " + g.service_slug, href: "/services/" + encodeURIComponent(g.service_slug) } : { label: "Make a service…", onClick: () => makeService(g) },
      { label: "Boot settings…", onClick: () => bootDrawer(g) },
      g.status === "running" ? { label: "Stop…", onClick: () => power(g, "stop") } : { label: "Start", onClick: () => power(g, "start") },
    ],
    empty: { title: "No guests", text: "Proxmox did not list any guests, or it cannot be reached. See Settings → Connections." },
  });

  // --- sections -----------------------------------------------------------------------------
  const nodesRegion = region($("#node-cards"), {
    initial: view,
    load: () => api.get("/api/views/nodes", { suppressToast: true }),
    render: (d) => { view = d; drawCards(); },
    topics: ["registry"],
  });
  const status = region(el("div"), {
    load: () => api.get("/api/infra/nodes/status", { suppressToast: true }),
    render: (d) => { live = d; drawCards(); },
    topics: ["run", "health"], wait: 3000, every: 30000, poll: true,
  });
  const guestsRegion = region(el("div"), {
    load: () => api.get("/api/infra/proxmox/guests", { suppressToast: true }),
    render: (d) => {
      guests = d; t.setRows(d); $("#guest-count").textContent = d.length; drawCards();
      // Loading the guests reads the boot settings of nodes that are on: show them on the cards.
      const unread = new Set(view.nodes.filter((n) => !(n.boot || {}).read_at).map((n) => n.name));
      if (d.some((g) => g.boot && g.boot.read_at && unread.has(g.node))) nodesRegion.refresh();
    },
    topics: ["run", "health", "registry"], wait: 3000, every: 30000, poll: true, age: guestAge,
  });
  const servicesRegion = region(el("div"), {
    initial: svcView,
    load: () => api.get("/api/views/services", { suppressToast: true }),
    render: (d) => { svcView = d; },
    topics: ["registry"],
  });

  $("#nodes-more").append(menuButton(() => [
    { label: "Add a node by hand…", onClick: () => nodeDrawer({ onSaved: () => nodesRegion.refresh() }) },
    { label: "Check all nodes", onClick: () => { status.refresh(); guestsRegion.refresh(); } },
  ], "More actions"));
})();
