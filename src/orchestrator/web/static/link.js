/* Linking both ways (SourceLink and ServiceWhere wireframes). Needs ui.js.
 *   Link.stack({instance, endpoint_id, name, env, sourceName, onDone})   a Portainer stack
 *   Link.guest({node, vmid, kind, name, sourceName, onDone})             a Proxmox guest
 *   Link.proxyHost({hostname, source, sourceName, service_slug, onDone}) a proxy host
 *     each opens "Link <thing> to a service" with An existing service | A new service.
 *   Link.where({service, parents, onDone})  the service page's "Where it runs": what it is part of, its guest per Proxmox,
 *                                   its stack per Portainer, its proxy hosts per proxy.
 * Everything goes through the registry API (PUT /api/registry/services/{slug}), the stack
 * adopt (/api/stacks/suggest, /api/stacks/adopt) and the proxy host links
 * (/api/services/{slug}/proxy-hosts). Nothing a remote system returns is put in as HTML. */
(function () {
  "use strict";
  const { el, clear, fill, api, drawer } = window.UI;
  const toast = (m, k) => window.toast(m, k);
  const enc = encodeURIComponent;
  const slugify = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const svcUrl = (slug) => "/api/registry/services/" + enc(slug);
  const get = (url) => api.get(url, { suppressToast: true });

  // the services and nodes (/api/views/services), read once per drawer
  const servicesView = () => get("/api/views/services");
  const sourcesOf = async (kind) => (await get("/api/sources")).filter((s) => s.kind === kind && s.enabled);
  const guestKind = (k) => (k === "lxc" || k === "ct" ? "ct" : "vm"); // the registry says ct
  const guestLabel = (g) => guestKind(g.kind) + "-" + g.vmid + " · " + g.name + " · " + g.node;
  const options = (list) => list.map((o) => el("option", { value: o.value }, o.label));

  // "An existing service | A new service": one drawer, the fields of the other mode hidden
  function modes(d, labels, saveLabels, notes) {
    const foot = d.el.querySelector("button[type=submit]");
    const note = d.el.querySelector(".drawer-foot .note");
    const bar = el("div", { class: "seg", role: "group", "aria-label": "Link to" });
    const draw = () => {
      fill(bar, Object.keys(labels).map((k) => el("button", { type: "button", "aria-pressed": String(d.values.mode === k), onclick: () => { d.set("mode", k); draw(); } }, labels[k])));
      foot.textContent = saveLabels[d.values.mode];
      note.textContent = notes[d.values.mode];
    };
    draw();
    return bar;
  }
  // the drawer's first field: the mode bar (filled once the drawer exists)
  const modeField = (holder) => ({ key: "mode_bar", type: "custom", render: () => holder });
  function linkDrawer(o) {
    const holder = el("div");
    const d = drawer({
      title: o.title, sub: o.sub, values: Object.assign({ mode: o.mode || "existing" }, o.values),
      fields: [modeField(holder)].concat(o.fields),
      saveLabel: o.saveLabels[o.mode || "existing"], note: o.notes[o.mode || "existing"],
      onSave: (v, h) => (v.mode === "new" ? o.onNew(v, h) : o.onExisting(v, h)),
    });
    holder.append(modes(d, { existing: "An existing service", new: "A new service" }, o.saveLabels, o.notes));
    const name = d.el.querySelector("#f-name"); const slug = d.el.querySelector("#f-slug");
    if (name && slug) {
      let touched = false;
      slug.addEventListener("input", () => { touched = true; });
      name.addEventListener("input", () => { if (!touched) { slug.value = slugify(name.value); d.set("slug", slug.value); } });
    }
    return d;
  }
  const isExisting = (v) => v.mode === "existing";
  const isNew = (v) => v.mode === "new";
  const serviceField = (rows, help) => ({ key: "service", label: "Service", type: "select", required: true, show: isExisting,
    options: [{ value: "", label: "Choose a service" }].concat(rows.map((r) => ({ value: r.slug, label: r.name + " (" + r.slug + ")" + (r.parent ? " · under " + r.parent : "") }))), help });
  const nameFields = (groups) => [
    { key: "name", label: "Name", required: true, show: isNew },
    { key: "slug", label: "Slug", required: true, mono: true, show: isNew, help: "Lower case, used in URLs and in services.yaml." },
    { key: "group", label: "Group", placeholder: "optional", show: isNew, list: groups ? "dl-link-groups" : null },
  ];
  const groupsList = (view) => el("datalist", { id: "dl-link-groups" }, (view.groups || []).map((g) => el("option", { value: g })));
  function done(o, msg) { toast(msg, "ok"); if (o.onDone) o.onDone(); }

  // --- a stack --------------------------------------------------------------------------------------
  async function stack(o) {
    const q = "endpoint=" + o.endpoint_id + "&name=" + enc(o.name) + (o.instance ? "&instance=" + enc(o.instance) : "");
    let g, view;
    try { [g, view] = await Promise.all([get("/api/stacks/suggest?" + q), servicesView()]); }
    catch (e) { toast(e.message, "error"); return; }
    const summary = el("div", { class: "card", style: { padding: "12px 14px" } });
    const drawSummary = (v) => fill(summary,
      el("div", { class: "lane" }, "The new service gets"),
      el("div", {}, "Runs as stack ", el("strong", {}, g.stack), " on ", el("strong", {}, o.sourceName || "Portainer"), o.env ? " · " + o.env : ""),
      g.children.length ? el("div", {}, "Sub-services for its " + g.children.length + " container" + (g.children.length === 1 ? "" : "s") + ": ", el("span", { class: "mono" }, g.children.map((c) => c.container).join(", "))) : null,
      v.probes ? el("div", {}, "A Portainer probe for each container, an HTTP probe through its first proxy host") : null,
      g.notes.length ? el("div", { class: "faint" }, g.notes.join(" ")) : null);
    const d = linkDrawer({
      title: "Link " + g.stack + " to a service", sub: (o.sourceName ? o.sourceName + " · " : "") + "stack",
      mode: g.already_linked.length ? "existing" : "new",
      values: {
        service: g.already_linked[0] || "", name: g.name || g.stack, slug: g.slug, group: "", parent: g.parent || "", probes: true,
        hosts: g.hosts.filter((h) => !h.linked_to || h.name_matches).map((h) => h.hostname),
      },
      fields: [
        serviceField(view.rows, "The stack becomes how it is started, stopped and redeployed (its controller). Its containers are not added; adopt it as a new service for that."),
        ...nameFields(true),
        { key: "parent", label: "Runs inside", type: "select", required: true, show: isNew,
          options: [{ value: "", label: "Choose the service Portainer runs on" }].concat(g.parents.map((p) => ({ value: p.slug, label: p.name + (p.guest ? " (" + p.guest + ")" : "") }))),
          help: "The new service is a sub-service of it: it lives on its guest and goes dark with it." },
        { key: "hosts", label: "Proxy hosts found pointing at this stack", type: "checklist", show: isNew, empty: "None points at a port this stack publishes.",
          options: g.hosts.map((h) => ({ value: h.hostname, label: h.hostname, note: "→ " + h.upstream + (h.linked_to ? " (now on " + h.linked_to + ": moves)" : "") })) },
        { key: "probes", label: "Add its probes", type: "checkbox", show: isNew },
        { key: "summary", type: "custom", show: isNew, render: (v) => { drawSummary(v); return summary; } },
      ],
      saveLabels: { existing: "Link", new: "Create service" },
      notes: { existing: "Links the stack to that service.", new: "Creates the service and links the stack. Everything can be changed on it afterwards." },
      async onExisting(v) {
        await api.put(svcUrl(v.service), { controller: { type: "portainer", endpoint: g.endpoint_id, stack: g.stack, instance: g.instance || o.instance || "" } }, { suppressToast: true });
        done(o, "Linked " + g.stack + " to " + v.service);
      },
      async onNew(v) {
        const r = await api.post("/api/stacks/adopt", {
          endpoint_id: g.endpoint_id, instance: g.instance || o.instance || "", stack: g.stack, slug: v.slug.trim(), name: v.name.trim(),
          parent: v.parent, group: (v.group || "").trim(), probes: !!v.probes, hosts: v.hosts, deploy: null, webhook: null,
          children: g.children.map((c) => ({ container: c.container, name: c.name, backup_gate: c.backup_gate })),
        }, { suppressToast: true });
        done(o, "Created " + r.services.join(", ") + (r.hosts.length ? " with " + r.hosts.join(", ") : ""));
      },
    });
    d.el.append(groupsList(view));
    // the summary follows the probes box
    d.el.querySelector("#f-probes").addEventListener("change", () => drawSummary(d.values));
  }

  // --- a guest --------------------------------------------------------------------------------------
  async function guest(o) {
    let view;
    try { view = await servicesView(); } catch (e) { toast(e.message, "error"); return; }
    const node = view.nodes.find((n) => n.name === o.node);
    const where = { node_id: node ? node.id : null, guest_kind: guestKind(o.kind), guest_id: o.vmid };
    const noNode = () => { const e = new Error("The node " + o.node + " is not recorded here yet: import it on the Nodes tab first."); throw e; };
    const d = linkDrawer({
      title: "Link " + o.name + " to a service", sub: (o.sourceName ? o.sourceName + " · " : "") + guestKind(o.kind).toUpperCase() + " " + o.vmid + " on " + o.node,
      mode: "new", values: { service: "", name: o.name, slug: slugify(o.name) || "vm-" + o.vmid, group: "" },
      fields: [
        serviceField(view.rows.filter((r) => !r.parent), "It then runs on this guest: woken, backed up and judged with it."),
        ...nameFields(true),
      ],
      saveLabels: { existing: "Link", new: "Create service" },
      notes: { existing: "The service moves to this guest.", new: "Creates the service on this guest. Probes are added on its page." },
      async onExisting(v) {
        if (!node) noNode();
        await api.put(svcUrl(v.service), where, { suppressToast: true });
        done(o, "Linked " + o.name + " to " + v.service);
      },
      async onNew(v) {
        if (!node) noNode();
        await api.post("/api/registry/services", Object.assign({ slug: v.slug.trim(), name: v.name.trim(), group: (v.group || "").trim() }, where), { suppressToast: true });
        done(o, "Created " + v.name.trim() + ". Add its probes on its page.");
      },
    });
    d.el.append(groupsList(view));
  }

  // --- a proxy host -----------------------------------------------------------------------------------
  async function proxyHost(o) {
    let view;
    try { view = await servicesView(); } catch (e) { toast(e.message, "error"); return; }
    const link = (slug) => api.post("/api/services/" + enc(slug) + "/proxy-hosts", { hostname: o.hostname, force: !!o.service_slug }, { suppressToast: true });
    const d = linkDrawer({
      title: "Link " + o.hostname + " to a service", sub: (o.sourceName ? o.sourceName + " · " : "") + "proxy host" + (o.service_slug ? " · now on " + o.service_slug : ""),
      values: { service: "", name: o.hostname.split(".")[0], slug: slugify(o.hostname.split(".")[0]), group: "", parent: "", node_id: view.nodes[0] ? String(view.nodes[0].id) : "" },
      fields: [
        serviceField(view.rows.filter((r) => r.slug !== o.service_slug), o.service_slug ? "It moves from " + o.service_slug + "." : "A visit to it can then wake the service, and its route is checked with it."),
        ...nameFields(true),
        { key: "parent", label: "Part of", type: "select", show: isNew, options: [{ value: "", label: "Nothing: it runs on a node" }].concat(view.rows.filter((r) => !r.parent).map((r) => ({ value: r.slug, label: r.name + " (" + r.slug + ")" }))) },
        { key: "node_id", label: "Node", type: "select", show: (v) => isNew(v) && !v.parent, required: true, options: view.nodes.map((n) => ({ value: String(n.id), label: n.name })) },
      ],
      saveLabels: { existing: "Link", new: "Create service" },
      notes: { existing: "The route changes on the next push, which happens by itself.", new: "Creates the service and links the proxy host." },
      async onExisting(v) { await link(v.service); done(o, "Linked " + o.hostname + " to " + v.service); },
      async onNew(v) {
        const body = { slug: v.slug.trim(), name: v.name.trim(), group: (v.group || "").trim() };
        if (v.parent) body.parent = v.parent; else Object.assign(body, { node_id: Number(v.node_id), guest_kind: "none" });
        await api.post("/api/registry/services", body, { suppressToast: true });
        await link(body.slug);
        done(o, "Created " + body.name + " with " + o.hostname);
      },
    });
    d.el.append(groupsList(view));
  }

  // --- where a service runs ---------------------------------------------------------------------------
  // S: the service (/api/views/service/<slug>.service). Guest and stack are saved with one PUT; the
  // proxy hosts added or removed here are linked and unlinked on Save.
  async function where(o) {
    const S = o.service;
    const ctl = S.controller || {};
    const container = ctl.type === "portainer" && !!ctl.container;
    let proxmoxes, portainers, proxies;
    try { [proxmoxes, portainers, proxies] = await Promise.all([sourcesOf("proxmox"), sourcesOf("portainer"), sourcesOf("proxy")]); }
    catch (e) { toast(e.message, "error"); return; }
    const guests = {};   // source id -> guests
    const stacks = {};   // source id -> {envs, stacks}
    const hostsOf = {};  // source id -> proxy hosts
    const hasGuest = S.guest_kind !== "none" && S.guest_id != null;
    const guestKey = (g) => g.node + "/" + g.vmid + "/" + guestKind(g.kind);
    const values = {
      pve: proxmoxes[0] ? proxmoxes[0].id : "", guest: hasGuest && S.node ? S.node.name + "/" + S.guest_id + "/" + S.guest_kind : "",
      pt: ctl.type === "portainer" && ctl.stack ? (ctl.instance || (portainers.find((p) => p.default) || portainers[0] || {}).id || "") : "",
      env: ctl.stack ? String(ctl.endpoint) : "", stack: ctl.stack || "",
      proxy: (proxies.find((p) => p.default) || proxies[0] || {}).id || "", add: "",
      links: (S.proxy_hosts || []).map((h) => h.hostname),
      parent: S.parent || "", backup_gate: S.backup_gate || "ignore", ip_address: S.ip_address || "",
    };
    const before = JSON.stringify([values.guest, values.pt, values.env, values.stack]);
    const root = (v) => !v.parent;

    // guest: Proxmox, then one of its guests
    const pveSel = el("select", { "aria-label": "Proxmox" }, options(proxmoxes.map((p) => ({ value: p.id, label: p.name }))));
    const guestSel = el("select", { "aria-label": "VM or container" });
    async function drawGuests(d) {
      const id = pveSel.value;
      fill(guestSel, el("option", { value: "" }, "loading…"));
      try { if (!guests[id]) guests[id] = await get("/api/infra/proxmox/guests?source=" + enc(id)); }
      catch (e) { fill(guestSel, el("option", { value: "" }, "could not be read: " + e.message)); return; }
      const list = guests[id];
      fill(guestSel, el("option", { value: "" }, "none: no guest"), options(list.map((g) => ({ value: guestKey(g), label: guestLabel(g) + (g.service_slug && g.service_slug !== S.slug ? " · " + g.service_slug : "") }))));
      if (d.values.guest && !list.some((g) => guestKey(g) === d.values.guest)) guestSel.append(el("option", { value: d.values.guest }, d.values.guest.replace(/\//g, " · ") + " (not in this Proxmox)"));
      guestSel.value = d.values.guest;
    }
    // stack: Portainer, environment, stack
    const ptSel = el("select", { "aria-label": "Portainer" }, el("option", { value: "" }, "none"), options(portainers.map((p) => ({ value: p.id, label: p.name }))));
    const envSel = el("select", { "aria-label": "Environment" });
    const stackSel = el("select", { "aria-label": "Stack" });
    async function drawStacks(d) {
      const id = ptSel.value;
      if (!id) { fill(envSel, el("option", { value: "" }, "—")); fill(stackSel, el("option", { value: "" }, "—")); envSel.disabled = stackSel.disabled = true; return; }
      envSel.disabled = stackSel.disabled = false;
      const inst = "instance=" + enc(id);
      try {
        if (!stacks[id]) {
          const [envs, st] = await Promise.all([get("/api/portainer/environments?" + inst), get("/api/portainer/stacks?" + inst)]);
          stacks[id] = { envs, stacks: st.stacks };
        }
      } catch (e) { fill(stackSel, el("option", { value: "" }, "could not be read: " + e.message)); return; }
      const s = stacks[id];
      fill(envSel, options(s.envs.map((e) => ({ value: String(e.id), label: e.name }))));
      if (!d.values.env || !s.envs.some((e) => String(e.id) === d.values.env)) d.set("env", s.envs[0] ? String(s.envs[0].id) : "");
      envSel.value = d.values.env;
      const mine = s.stacks.filter((x) => String(x.endpoint_id) === envSel.value);
      fill(stackSel, el("option", { value: "" }, "choose a stack"), options(mine.map((x) => ({ value: x.name, label: x.name + ((x.linked_services || []).filter((y) => y !== S.slug).length ? " · " + x.linked_services.join(", ") : "") }))));
      stackSel.value = d.values.stack;
    }
    // proxy hosts: what it has, and one to add from a proxy
    const hostList = el("div");
    const proxySel = el("select", { "aria-label": "Proxy" }, options(proxies.map((p) => ({ value: p.id, label: p.name }))));
    const addSel = el("select", { "aria-label": "Add a proxy host" });
    const newLink = el("a", {}, "create a new proxy host");
    function drawLinks(d) {
      clear(hostList);
      if (!d.values.links.length) hostList.append(el("p", { class: "faint", style: { margin: "0 0 8px" } }, "None."));
      for (const h of d.values.links) {
        hostList.append(el("div", { class: "event-row", style: { gridTemplateColumns: "minmax(0,1fr) auto" } },
          el("span", { class: "mono" }, h),
          el("button", { type: "button", class: "ghost small", "aria-label": "Unlink " + h, onclick: () => { d.set("links", d.values.links.filter((x) => x !== h)); drawLinks(d); drawAdd(d); } }, "✕")));
      }
    }
    async function drawAdd(d) {
      const id = proxySel.value;
      newLink.href = id ? "/sources/" + enc(id) + "?tab=hosts&new=1&service_id=" + S.id : "/find/proxy-host?new=1&service_id=" + S.id;
      if (!id) { fill(addSel, el("option", { value: "" }, "no proxy")); return; }
      try { if (!hostsOf[id]) hostsOf[id] = await get("/api/proxy-hosts?source=" + enc(id)); }
      catch (e) { fill(addSel, el("option", { value: "" }, "could not be read: " + e.message)); return; }
      const free = hostsOf[id].filter((h) => !d.values.links.includes(h.hostname));
      fill(addSel, el("option", { value: "" }, free.length ? "choose a proxy host" : "nothing more on this proxy"),
        options(free.map((h) => ({ value: h.hostname, label: h.hostname + (h.service_slug && h.service_slug !== S.slug ? " · on " + h.service_slug : " · not linked") }))));
    }
    const row = (...kids) => el("div", { style: { display: "grid", gridTemplateColumns: "repeat(" + kids.length + ", minmax(0,1fr))", gap: "8px" } }, kids);
    const labelled = (text, input) => el("label", { class: "label", style: { display: "grid", gap: "4px" } }, text, input);

    const d = drawer({
      title: "Where it runs", sub: S.name || S.slug, values, saveLabel: "Save",
      note: "Applies as soon as you save.",
      fields: [
        { key: "parent", label: "Part of", type: "select", options: [{ value: "", label: "Nothing: it has its own guest" }].concat((o.parents || []).map((x) => ({ value: x, label: x }))),
          help: "A sub-service lives on its parent's node and guest and goes dark with it." },
        { key: "backup_gate", label: "Its parent's backup waits for it", type: "select", show: (v) => !root(v),
          options: [{ value: "ignore", label: "No: never checked before a backup" }, { value: "required", label: "Yes: must be healthy first (started if stopped)" }] },
        { type: "section", label: "Guest", show: root },
        { key: "guest_ui", type: "custom", show: root, render: () => [row(labelled("Proxmox", pveSel), labelled("VM or container", guestSel)),
          proxmoxes.length ? null : el("p", { class: "faint" }, "No Proxmox source: add one on Sources.")] },
        { type: "section", label: "Container stack" },
        container ? { key: "ctl_ui", type: "custom", render: () => el("p", { class: "faint", style: { margin: 0 } }, "Runs as the container ", el("span", { class: "mono" }, ctl.container), " (environment " + ctl.endpoint + "). Its controller is under Wake and power.") }
          : { key: "stack_ui", type: "custom", render: () => [row(labelled("Portainer", ptSel), labelled("Environment", envSel), labelled("Stack", stackSel)),
            el("p", { class: "faint", style: { margin: "6px 0 0", fontSize: "13px" } }, "None: it runs straight on its guest, not in a stack.")] },
        { type: "section", label: "Proxy hosts" },
        { key: "hosts_ui", type: "custom", render: () => [hostList,
          el("div", { style: { display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1.6fr) auto", gap: "8px", alignItems: "end" } },
            labelled("Proxy", proxySel), labelled("Add a proxy host", addSel),
            el("button", { type: "button", class: "secondary", onclick: () => { if (!addSel.value) return; d.set("links", d.values.links.concat(addSel.value)); drawLinks(d); drawAdd(d); } }, "Add")),
          el("p", { class: "faint", style: { margin: "6px 0 0", fontSize: "13px" } }, "Pick one this proxy already has, or ", newLink, " on it.")] },
        { type: "section", label: "Address" },
        { key: "ip_address", label: "Address by hand", mono: true, placeholder: (S.ip && S.ip.discovered_ip) || "blank = the one found", help: "Wins over the address found through Proxmox, the guest agent, ARP or DHCP." },
      ].filter(Boolean),
      async onSave(v) {
        const body = {};
        if ((v.ip_address || "").trim() !== (S.ip_address || "")) body.ip_address = (v.ip_address || "").trim();
        if (v.parent !== (S.parent || "")) body.parent = v.parent;
        if (v.parent && v.backup_gate !== (S.backup_gate || "ignore")) body.backup_gate = v.backup_gate;
        if (JSON.stringify([v.guest, v.pt, v.env, v.stack]) !== before) {
          if (root(v)) {
            if (!v.guest) Object.assign(body, { guest_kind: "none", guest_id: null });
            else {
              const [nodeName, vmid, kind] = v.guest.split("/");
              const view = await servicesView();
              const node = view.nodes.find((n) => n.name === nodeName);
              if (!node) throw new Error("The node " + nodeName + " is not recorded here yet: import it on its Proxmox page first.");
              Object.assign(body, { node_id: node.id, guest_kind: kind, guest_id: Number(vmid) });
            }
          }
          if (!container) {
            if (v.pt && v.stack) body.controller = { type: "portainer", endpoint: Number(v.env), stack: v.stack, instance: v.pt };
            else if (v.pt && !v.stack) { const e = new Error("Choose a stack, or set Portainer to none."); throw e; }
            else if (ctl.type === "portainer" && ctl.stack) body.controller = {};
          }
        }
        if (Object.keys(body).length) await api.put(svcUrl(S.slug), body, { suppressToast: true });
        const had = (S.proxy_hosts || []).map((h) => h.hostname);
        const failed = [];
        for (const h of had.filter((x) => !v.links.includes(x))) {
          try { await api.del("/api/services/" + enc(S.slug) + "/proxy-hosts/" + enc(h), { suppressToast: true }); } catch (e) { failed.push(h + ": " + e.message); }
        }
        for (const h of v.links.filter((x) => !had.includes(x))) {
          try { await api.post("/api/services/" + enc(S.slug) + "/proxy-hosts", { hostname: h, force: true }, { suppressToast: true }); } catch (e) { failed.push(h + ": " + e.message); }
        }
        if (failed.length) toast(failed.join(" · "), "error"); else toast("Saved", "ok");
        if (o.onDone) o.onDone();
      },
    });
    // the guest that is linked now picks its Proxmox: the one that lists it
    pveSel.addEventListener("change", () => drawGuests(d));
    guestSel.addEventListener("change", () => d.set("guest", guestSel.value));
    ptSel.addEventListener("change", () => { d.set("pt", ptSel.value); d.set("env", ""); d.set("stack", ""); drawStacks(d); });
    envSel.addEventListener("change", () => { d.set("env", envSel.value); d.set("stack", ""); drawStacks(d); });
    stackSel.addEventListener("change", () => d.set("stack", stackSel.value));
    proxySel.addEventListener("change", () => drawAdd(d));
    ptSel.value = values.pt; proxySel.value = values.proxy;
    drawLinks(d); drawAdd(d); drawStacks(d);
    if (proxmoxes.length) {
      if (values.guest && proxmoxes.length > 1) {
        for (const p of proxmoxes) {
          try { guests[p.id] = await get("/api/infra/proxmox/guests?source=" + enc(p.id)); } catch (_e) { continue; }
          if (guests[p.id].some((g) => guestKey(g) === values.guest)) { pveSel.value = p.id; break; }
        }
      }
      drawGuests(d);
    }
  }

  window.Link = { stack, guest, proxyHost, where };
})();
