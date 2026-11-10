/* A Proxmox source's own tabs (source.js loads them through window.SourceViews), to the
 * SourceProxmox and SourceGuests wireframes. Needs objects.js.
 *   proxmox.nodes   one card per node of this cluster: power, its own API, guests, services,
 *                   wake on hit, boot sequence; wake, restart, power off, edit, delete.
 *   proxmox.guests  every guest of this cluster: state, start at boot, disk, service; start,
 *                   stop, boot settings, make a service.
 * Node cards paint from the database (/api/views/nodes?source=), their live state from
 * /api/infra/nodes/status?source= and the guests from /api/infra/proxmox/guests?source=; guest
 * actions go to the cluster that owns the node. Nothing from Proxmox is put in as HTML. */
(function () {
  "use strict";
  const { el, clear, fill, api, fmt, region, table, menuButton, confirm, drawer } = window.UI;
  const { nodeDrawer, nodePower, deleteNode, serviceDrawer, bulk } = window.Objects;
  const enc = encodeURIComponent;
  window.SourceViews = window.SourceViews || {};

  // --- Nodes ----------------------------------------------------------------------------------------
  window.SourceViews["proxmox.nodes"] = function (panel, ctx) {
    const q = "?source=" + enc(ctx.source.id);
    let view = null;   // /api/views/nodes
    let live = null;   // /api/infra/nodes/status
    let guests = null; // /api/infra/proxmox/guests
    const box = el("div", { class: "node-grid", "aria-live": "polite" });
    fill(panel, el("div", { style: { display: "flex", gap: "8px", justifyContent: "flex-end", marginBottom: "12px" } },
      el("a", { class: "btn secondary small", href: "/pull#proxmox" }, "Import from Proxmox"),
      menuButton(() => [
        { label: "Add a node by hand…", onClick: () => nodeDrawer({ onSaved: () => nodesRegion.refresh() }) },
        { label: "Check all nodes", onClick: () => status.refresh() },
      ], "More actions")), box);

    const upFor = (s) => { if (!s) return ""; const d = Math.floor(s / 86400); const h = Math.floor((s % 86400) / 3600); return d ? d + " d" : h + " h"; };
    const liveOf = (name) => live && live.nodes.find((n) => n.name === name);
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
    function bootText(node) {
      const b = node.boot || {};
      if (!b.read_at) return el("span", { class: "faint" }, "not read yet: it is read while the node is on");
      const n = b.sequence.length;
      return (n ? n + " guest" + (n === 1 ? "" : "s") + " start by themselves" : "no guest starts by itself") + (b.delay_s ? " · after " + fmt.secs(b.delay_s) : "");
    }
    function guestText(node) {
      if (!guests) return "…";
      const mine = guests.filter((g) => g.node === node.name);
      return mine.length + " (" + mine.filter((g) => g.status === "running").length + " running)";
    }
    const fact = (label, value, mono) => el("div", {}, el("div", { class: "faint", style: { fontSize: "12px" } }, label), el("div", { class: mono ? "mono" : null }, value == null || value === "" ? "—" : value));

    function draw() {
      if (!view) return;
      clear(box);
      ctx.setCount(view.nodes.length);
      if (!view.nodes.length) {
        box.append(el("div", { class: "card dt-empty" }, el("strong", {}, "No nodes yet"),
          el("div", {}, "Import them from Proxmox: every node and guest of the cluster, in one go."),
          el("div", { style: { marginTop: "10px" } }, el("a", { class: "btn small", href: "/pull#proxmox" }, "Import from Proxmox"))));
        return;
      }
      for (const n of view.nodes) {
        const st = stateOf(n);
        const canWake = !n.always_on && !!n.power_mgr_target;
        const refresh = () => { status.refresh(); nodesRegion.refresh(); };
        const primary = st.label === "off" && canWake
          ? el("button", { type: "button", onclick: () => nodePower(n, "wake", refresh) }, "Wake")
          : el("button", { type: "button", class: "secondary", onclick: () => status.refresh() }, "Check");
        box.append(el("article", { class: "card node-card", "aria-label": n.name },
          el("div", { class: "card-head" },
            el("h2", { class: "mono", style: { fontSize: "17px", fontFamily: "var(--mono)" } }, n.name),
            el("span", { class: "pill " + st.cls, title: st.why || null }, st.label),
            el("span", { class: "faint", style: { fontSize: "12.5px" } }, n.always_on ? "always on" : canWake ? "wakes on demand" : "no power target"),
            el("span", { class: "node-acts" }, primary, menuButton(() => [
              { label: "Edit…", onClick: () => nodeDrawer({ node: n, onSaved: refresh }) },
              { label: "Check now", onClick: () => status.refresh() },
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
            fact("Services", el("a", { href: "/services?group=node&q=" + enc(n.name) }, String(n.services))),
            fact("Wake on hit", n.always_on ? "not needed (always on)" : !n.power_mgr_target ? "no power target" : n.wake_on_hit_enabled ? "yes" : "no"),
            fact("When it boots", bootText(n)),
            n.always_on ? null : fact("Boot takes", "about " + fmt.secs(n.boot.boot_s) + (n.boot.boot_learned ? " (its last wakes)" : " (a guess until it has been woken)"))),
          n.notes ? el("p", { class: "faint", style: { margin: "12px 0 0", fontSize: "13px" } }, n.notes) : null));
      }
    }
    const nodesRegion = region(box, {
      load: () => api.get("/api/views/nodes" + q, { suppressToast: true }),
      render: (d) => { view = d; draw(); }, topics: ["registry"],
    });
    const status = region(el("div"), {
      load: () => api.get("/api/infra/nodes/status" + q, { suppressToast: true }),
      render: (d) => { live = d; draw(); }, topics: ["run", "health"], wait: 3000, every: 30000, poll: true,
    });
    region(el("div"), {
      load: () => api.get("/api/infra/proxmox/guests" + q, { suppressToast: true }),
      render: (d) => { guests = d; draw(); }, every: 60000,
    });
  };

  // --- Guests ---------------------------------------------------------------------------------------
  window.SourceViews["proxmox.guests"] = function (panel, ctx) {
    const q = "?source=" + enc(ctx.source.id);
    let svcView = null; // /api/views/services, for "make a service"
    const box = el("div");
    const age = el("span", { class: "age" });
    const nodeSel = el("select", { "aria-label": "Node" }, el("option", { value: "" }, "Node: all"));
    fill(panel, box);
    const serviceOf = (slug) => svcView && svcView.rows.find((r) => r.slug === slug);

    async function makeService(g) {
      if (!svcView) svcView = await api.get("/api/views/services", { suppressToast: true });
      const node = svcView.nodes.find((n) => n.name === g.node);
      serviceDrawer({
        view: svcView,
        prefill: { name: g.name, node_id: node ? node.id : null, guest_kind: g.kind === "lxc" ? "lxc" : "vm", guest_id: g.vmid },
        onSaved: () => { svcView = null; guestsRegion.refresh(); },
      });
    }
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
          await api.put("/api/infra/guests/" + enc(g.node) + "/" + g.vmid + "/boot",
            { onboot: !!v.onboot, order: v.onboot ? num(v.order) : null, up_s: v.onboot ? num(v.up_s) : null, down_s: num(v.down_s) }, { suppressToast: true });
          window.toast("Saved " + g.name + "'s boot settings in Proxmox", "ok");
          guestsRegion.refresh();
        },
      });
    }
    async function power(rows, action) {
      if (action === "stop") {
        if (!svcView) svcView = await api.get("/api/views/services", { suppressToast: true }).catch(() => null);
        const used = rows.map((g) => serviceOf(g.service_slug)).filter(Boolean);
        const ok = await confirm({
          title: rows.length === 1 ? "Stop " + rows[0].kind.toUpperCase() + " " + rows[0].vmid + " (" + rows[0].name + ")?" : "Stop " + rows.length + " guests?",
          body: (used.length ? used.map((s) => s.name).join(", ") + (used.length === 1 ? " runs" : " run") + " on " + (rows.length === 1 ? "it" : "them") + " and stops too. " : "") + "They are shut down cleanly.",
          affectedLabel: rows.length > 1 ? "Guests" : null, affected: rows.length > 1 ? rows.map((g) => g.vmid + " " + g.name) : null,
          action: "Stop",
        });
        if (!ok) return;
      }
      for (const g of rows) {
        try {
          await api.post("/api/infra/guests/" + enc(g.node) + "/" + g.vmid + "/power", { action }, { suppressToast: true });
          window.toast((action === "start" ? "Starting " : "Stopping ") + g.name, "ok");
        } catch (e) { window.toast(g.name + ": " + e.message, "error"); }
      }
      t.clearSelection();
      setTimeout(() => guestsRegion.refresh(), 2500);
    }
    // several guests' Start at boot at once; each keeps its own order and waits
    function bootMany(rows) {
      drawer({
        title: "Start at boot for " + rows.length + " guest" + (rows.length === 1 ? "" : "s"),
        sub: "Written to Proxmox for each one (its node must be on). Their order and waits stay as they are.",
        values: { onboot: true },
        saveLabel: "Save in Proxmox",
        fields: [{ key: "onboot", label: "Proxmox starts them when their node boots", type: "checkbox" }],
        async onSave(v) {
          const failed = [];
          for (const g of rows) {
            const b = g.boot || {};
            try {
              await api.put("/api/infra/guests/" + enc(g.node) + "/" + g.vmid + "/boot",
                { onboot: !!v.onboot, order: v.onboot ? b.order ?? null : null, up_s: v.onboot ? b.up_s ?? null : null, down_s: b.down_s ?? null }, { suppressToast: true });
            } catch (e) { failed.push(g.name + ": " + e.message); }
          }
          window.toast(failed.length ? failed.join(" · ") : "Saved for " + rows.length + " guest" + (rows.length === 1 ? "" : "s"), failed.length ? "error" : "ok");
          t.clearSelection(); guestsRegion.refresh();
        },
      });
    }
    async function checkDisks() {
      try { await api.post("/api/infra/disks/check", undefined, { suppressToast: true }); window.toast("Disks checked", "ok"); }
      catch (e) { window.toast(e.message, "error"); return; }
      guestsRegion.refresh();
    }
    function diskSort(g) {
      const d = g.disk;
      if (!d || !d.worst) return d && d.state === "unmeasured" ? -1 : -2;
      return d.worst.percent;
    }
    function diskCell(g) {
      const d = g.disk;
      if (!d) return el("span", { class: "faint" }, "not checked yet");
      if (d.state === "off") return el("span", { class: "faint" }, "—");
      if (d.state === "unmeasured") return el("span", { class: "faint", title: d.error || "" }, g.kind === "lxc" || g.kind === "ct" ? "not measured" : "not measured: no guest agent");
      const w = d.worst || {};
      const others = (d.filesystems || []).filter((f) => f.mount !== w.mount);
      return [
        el("span", { class: "pill " + ({ full: "fail", low: "warn" }[d.state] || "ok"), title: (d.filesystems || []).map((f) => f.mount + "  " + f.percent + "%  " + fmt.bytes(f.used) + " of " + fmt.bytes(f.total)).join("\n") }, Math.round(w.percent) + "% " + w.mount),
        el("span", { class: "sub" }, fmt.bytes(w.used) + " of " + fmt.bytes(w.total) + (others.length ? " · " + others.length + " more" : "")),
      ];
    }
    const kindOf = (g) => (g.kind === "ct" || g.kind === "lxc" ? "LXC" : g.kind === "vm" ? "VM" : g.kind);
    let all = [];
    const shown = () => (nodeSel.value ? all.filter((g) => g.node === nodeSel.value) : all);
    const t = table(box, {
      // ?q= filters it (an alert about a guest lands here with its VMID)
      rows: [], key: (g) => g.node + "/" + g.vmid, name: (g) => g.name, noun: ["guest", "guests"], urlState: true,
      placeholder: "Filter guests", age, toolbarExtra: nodeSel,
      search: (g) => [g.vmid, g.name, g.node, g.kind, g.service_slug || ""].join(" "),
      sort: { key: "vmid", dir: 1 },
      columns: [
        { key: "vmid", label: "Guest", sort: (g) => g.vmid, render: (g) => [el("strong", { class: "mono" }, kindOf(g).toLowerCase().replace("lxc", "ct") + "-" + g.vmid), " ", el("span", { class: "faint" }, g.name)] },
        { key: "node", label: "Node", sort: true },
        { key: "status", label: "State", sort: true, render: (g) => el("span", { class: "pill " + (g.status === "running" ? "ok" : "off") }, g.status) },
        { key: "boot", label: "Starts with node", sort: (g) => (g.boot && g.boot.onboot ? g.boot.position || 99 : 999), phone: "Starts with node",
          render: (g) => !g.boot ? el("span", { class: "faint" }, "not read yet")
            : !g.boot.onboot ? el("span", { class: "faint" }, "no: started when needed")
            : [el("span", {}, "yes, " + fmt.nth(g.boot.position || 0)), el("span", { class: "sub" }, [g.boot.order != null ? "order " + g.boot.order : "no order", g.boot.up_s ? "waits " + g.boot.up_s + " s" : "", g.boot.starts_after_s != null ? "starts ~" + fmt.secs(g.boot.starts_after_s) + " after boot" : ""].filter(Boolean).join(" · "))] },
        { key: "service", label: "Service", sort: (g) => g.service_slug || "~",
          render: (g) => g.service_slug ? el("a", { href: "/services/" + enc(g.service_slug) }, g.service_slug)
            : [el("span", { class: "faint" }, "not linked"), " · ", el("button", { type: "button", class: "ghost small", style: { color: "var(--link)", padding: "0" }, onclick: () => makeService(g) }, "Make a service…")] },
        { key: "disk", label: "Disk", sort: diskSort, phone: "Disk", render: diskCell },
      ],
      chips: [
        { key: "running", label: "Running", test: (g) => g.status === "running" },
        { key: "stopped", label: "Stopped", test: (g) => g.status !== "running" },
        { key: "unlinked", label: "Not linked", test: (g) => !g.service_slug },
        { key: "disk", label: "Disk filling up", test: (g) => !!g.disk && (g.disk.state === "low" || g.disk.state === "full") },
      ],
      menu: (g) => [
        g.service_slug ? { label: "Open " + g.service_slug, href: "/services/" + enc(g.service_slug) } : { label: "Make a service…", onClick: () => makeService(g) },
        { label: "Boot settings…", onClick: () => bootDrawer(g) },
        g.status === "running" ? { label: "Shut down…", onClick: () => power([g], "stop") } : { label: "Start", onClick: () => power([g], "start") },
      ],
      bulk: (rows) => [
        { label: "Start", onClick: () => power(rows.filter((g) => g.status !== "running"), "start") },
        { label: "Shut down…", onClick: () => power(rows.filter((g) => g.status === "running"), "stop") },
        { label: "Start at boot…", onClick: () => bootMany(rows) },
        { label: "Check disks", onClick: checkDisks },
      ],
      empty: { title: "No guests", text: ctx.source.name + " lists no guests, or cannot be reached." },
    });
    nodeSel.addEventListener("change", () => t.setRows(shown()));
    const guestsRegion = region(el("div"), {
      load: () => api.get("/api/infra/proxmox/guests" + q, { suppressToast: true }),
      render: (d) => {
        all = d; ctx.setCount(d.length);
        const keep = nodeSel.value;
        fill(nodeSel, el("option", { value: "" }, "Node: all"), Array.from(new Set(d.map((g) => g.node))).sort().map((n) => el("option", { value: n }, "Node: " + n)));
        nodeSel.value = keep;
        t.setRows(shown());
      },
      onError: (e) => window.toast(e.message, "error"),
      topics: ["run", "health", "registry"], wait: 3000, every: 30000, poll: true, age,
    });
  };
})();
