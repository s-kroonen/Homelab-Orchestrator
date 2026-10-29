/* One drawer and one set of actions per object, used by every page that shows that object
 * (docs/design/README.md, rule 2). Needs ui.js.
 *
 *   Objects.bulk(resource, action, ids, params)    POST /api/bulk/<resource>, one toast summing up
 *   Objects.serviceDrawer({service, view, prefill, onSaved})   create (no service) or edit
 *   Objects.serviceActions(row, view, refresh)     the ⋯ menu of a service row
 *   Objects.deleteService(row, view, refresh)
 *   Objects.nodeDrawer({node, onSaved})            create (no node) or edit
 *   Objects.nodePower(node, action, view, refresh) wake / restart / power off with a confirm
 */
(function () {
  "use strict";
  const { el, api, drawer, confirm } = window.UI;
  const toast = (m, k) => window.toast(m, k);

  // --- bulk ----------------------------------------------------------------------------------
  const VERBS = {
    resync: "Checked", wake: "Woken", backup: "Backup started for", enable: "Enabled", disable: "Disabled",
    update: "Updated", discover_ip: "Looked for the address of", ctl_restart: "Restarted", ctl_start: "Started", ctl_stop: "Stopped",
    power_off: "Power off sent to", delete: "Deleted", wake_on_hit_on: "Wake on hit on for", wake_on_hit_off: "Wake on hit off for",
  };
  async function bulk(resource, action, ids, params) {
    const r = await api.post("/api/bulk/" + resource, { action, ids, params: params || {} });
    const verb = VERBS[action] || "Done:";
    const failed = r.results.filter((x) => !x.ok);
    if (!failed.length) {
      toast(ids.length === 1 ? verb + " " + ids[0] + (r.results[0].message && action !== "enable" && action !== "disable" ? ": " + r.results[0].message : "")
        : verb + " " + r.ok_count + " " + resource.replace("-", " "), "ok");
    } else {
      toast((r.ok_count ? r.ok_count + " done, " : "") + failed.length + " failed: " +
        failed.slice(0, 3).map((x) => x.id + ": " + x.message).join("; "), "error");
    }
    return r;
  }

  // --- services ------------------------------------------------------------------------------
  const slugify = (s) => String(s || "").toLowerCase().normalize("NFKD").replace(/[^\w\s-]/g, "")
    .trim().replace(/[\s_]+/g, "-").replace(/-+/g, "-").slice(0, 63);

  // view: {nodes, policies, groups, guest_kinds, rows}; service: a row of /api/views/services
  function serviceDrawer(o) {
    const view = o.view; const svc = o.service; const editing = !!svc;
    const pre = Object.assign({}, o.prefill || {});
    const groupsList = "dl-groups-" + Math.random().toString(36).slice(2);
    const values = editing ? {
      name: svc.name, slug: svc.slug, parent: svc.parent || "", node_id: svc.node_id == null ? "" : String(svc.node_id),
      guest_kind: svc.guest_kind, guest_id: svc.guest_id, group: svc.group || "",
      backup_policy_id: svc.backup_policy_id == null ? "" : String(svc.backup_policy_id),
      enabled: svc.enabled, wake_on_hit_enabled: svc.wake_on_hit_enabled,
    } : {
      name: pre.name || "", slug: pre.slug || slugify(pre.name), parent: pre.parent || "", node_id: pre.node_id != null ? String(pre.node_id) : (view.nodes[0] ? String(view.nodes[0].id) : ""),
      guest_kind: pre.guest_kind || "vm", guest_id: pre.guest_id == null ? null : pre.guest_id, group: "",
      backup_policy_id: "", enabled: true, wake_on_hit_enabled: true,
    };
    let slugTouched = editing || !!pre.slug;
    const others = (view.rows || []).filter((r) => !svc || (r.slug !== svc.slug));
    const d = drawer({
      title: editing ? "Edit " + svc.name : "New service",
      sub: editing ? svc.slug : "Probes, dependencies and its proxy hosts are set on its page afterwards.",
      values,
      saveLabel: editing ? "Save" : "Create service",
      note: "Saved at once; services.yaml is written by itself.",
      fields: [
        { key: "name", label: "Name", required: true, placeholder: "Media server" },
        { key: "slug", label: "Slug", required: true, mono: true, readonly: editing, help: editing ? "The slug cannot change: other services refer to it." : "Lower case, used in URLs and in services.yaml." },
        { type: "section", label: "Where it runs" },
        { key: "parent", label: "Part of", type: "select", options: [{ value: "", label: "Nothing: it has its own guest" }].concat(others.filter((r) => !r.parent).map((r) => ({ value: r.slug, label: r.name + " (" + r.slug + ")" }))), help: "A sub-service (a container, an add-on) runs inside its parent and follows it." },
        { key: "node_id", label: "Node", type: "select", options: view.nodes.map((n) => ({ value: String(n.id), label: n.name })), show: (v) => !v.parent, required: true },
        { key: "guest_kind", label: "Guest", type: "select", options: view.guest_kinds.map((k) => ({ value: k, label: { vm: "VM", lxc: "LXC container", none: "None (not a Proxmox guest)" }[k] || k })), show: (v) => !v.parent },
        { key: "guest_id", label: "VMID", type: "number", mono: true, show: (v) => !v.parent && v.guest_kind !== "none", required: true, placeholder: "104" },
        { type: "section", label: "Behaviour" },
        { key: "group", label: "Group", placeholder: "optional", list: groupsList },
        { key: "backup_policy_id", label: "Backup policy", type: "select", options: [{ value: "", label: "None" }].concat(view.policies.map((p) => ({ value: String(p.id), label: p.name }))) },
        { key: "enabled", label: "Enabled: checked, woken and backed up", type: "checkbox" },
        { key: "wake_on_hit_enabled", label: "Wake on hit: a visit to one of its hosts wakes it", type: "checkbox" },
      ],
      async onSave(v) {
        const body = {
          name: v.name.trim(), group: (v.group || "").trim(), enabled: !!v.enabled,
          backup_policy_id: v.backup_policy_id === "" ? null : Number(v.backup_policy_id),
        };
        if (v.parent) body.parent = v.parent;
        else {
          body.node_id = v.node_id === "" ? null : Number(v.node_id);
          body.guest_kind = v.guest_kind;
          body.guest_id = v.guest_kind === "none" ? null : v.guest_id;
          if (editing) body.parent = "";
        }
        if (editing) {
          body.wake_on_hit_enabled = !!v.wake_on_hit_enabled;
          await api.put("/api/registry/services/" + encodeURIComponent(svc.slug), body, { suppressToast: true });
          toast("Saved " + body.name, "ok");
        } else {
          body.slug = v.slug.trim();
          await api.post("/api/registry/services", body, { suppressToast: true });
          if (!v.wake_on_hit_enabled) {
            await api.put("/api/registry/services/" + encodeURIComponent(body.slug), { wake_on_hit_enabled: false }, { suppressToast: true });
          }
          toast("Created " + body.name + ". Add its probes on its page.", "ok");
        }
        if (o.onSaved) o.onSaved(v);
      },
    });
    d.el.append(el("datalist", { id: groupsList }, (view.groups || []).map((g) => el("option", { value: g }))));
    if (!editing) {
      const name = d.el.querySelector("#f-name"); const slug = d.el.querySelector("#f-slug");
      slug.addEventListener("input", () => { slugTouched = true; });
      name.addEventListener("input", () => { if (!slugTouched) { slug.value = slugify(name.value); d.set("slug", slug.value); } });
    }
    return d;
  }

  async function deleteService(row, view, refresh) {
    const kids = (view.rows || []).filter((r) => r.parent === row.slug).map((r) => r.slug);
    const ok = await confirm({
      title: "Delete " + row.name + "?",
      body: "It goes to the Archive for 30 days and can be restored from there. Its probes and proxy-host links go with it; its backups in PBS stay.",
      affected: kids.length ? kids.map((k) => "sub-service " + k + " is deleted with it") : null,
      typeName: row.slug, action: "Delete",
    });
    if (!ok) return;
    await api.del("/api/registry/services/" + encodeURIComponent(row.slug) + (kids.length ? "?cascade=true" : ""));
    toast("Deleted " + row.name + " (in the Archive for 30 days)", "ok");
    if (refresh) refresh();
  }

  function serviceActions(row, view, refresh) {
    const after = () => { if (refresh) refresh(); };
    const one = (action, params) => () => bulk("services", action, [row.slug], params).then(after, () => {});
    return [
      { label: "Open", href: "/services/" + encodeURIComponent(row.slug) },
      { label: "Check now", onClick: one("resync") },
      row.state.label === "asleep" || row.state.label === "stopped" || row.state.label === "unknown" ? { label: "Wake", onClick: one("wake") } : null,
      row.has_controller ? { label: "Restart", onClick: async () => {
        if (await confirm({ title: "Restart " + row.name + "?", body: "Anyone using it is cut off while it restarts.", action: "Restart", danger: false })) one("ctl_restart")();
      } } : null,
      row.backup_policy_id != null && !row.parent ? { label: "Run a backup now", onClick: one("backup") } : null,
      { label: "Edit settings…", onClick: () => serviceDrawer({ service: row, view, onSaved: after }) },
      { label: "Its activity", href: "/activity?service=" + encodeURIComponent(row.slug) },
      { label: row.enabled ? "Disable" : "Enable", onClick: one(row.enabled ? "disable" : "enable") },
      "-",
      { label: "Delete…", danger: true, onClick: () => deleteService(row, view, refresh).catch(() => {}) },
    ];
  }

  // --- nodes ---------------------------------------------------------------------------------
  function nodeDrawer(o) {
    const n = o.node; const editing = !!n;
    return drawer({
      title: editing ? "Edit " + n.name : "Add a node",
      sub: editing ? "A Proxmox machine" : "Nodes are usually imported from Proxmox; add one by hand only if it is not in the cluster.",
      values: editing ? {
        name: n.name, address: n.address, power_mgr_target: n.power_mgr_target, always_on: n.always_on,
        wake_on_hit_enabled: n.wake_on_hit_enabled, notes: n.notes,
      } : { name: "", address: "", power_mgr_target: "", always_on: false, wake_on_hit_enabled: true, notes: "" },
      saveLabel: editing ? "Save" : "Add node",
      note: "Saved at once; services.yaml is written by itself.",
      fields: [
        { key: "name", label: "Name", required: true, mono: true, help: editing ? "As Proxmox calls it. Renaming here does not rename it in Proxmox." : "As Proxmox calls it." },
        { key: "address", label: "Proxmox address", mono: true, placeholder: "10.0.0.5 or 10.0.0.5:8006", help: "Where its own Proxmox API answers, for checking it." },
        { type: "section", label: "Power" },
        { key: "always_on", label: "Always on: never powered off by the orchestrator", type: "checkbox" },
        { key: "power_mgr_target", label: "Power target", mono: true, placeholder: "node-b", help: "What the power manager calls it (an MQTT topic suffix, a host name). Empty: it cannot be woken.", show: (v) => !v.always_on },
        { key: "wake_on_hit_enabled", label: "Wake on hit: a visit to one of its services may wake it", type: "checkbox" },
        { key: "notes", label: "Notes", type: "textarea", rows: 3 },
      ],
      async onSave(v) {
        const body = {
          name: v.name.trim(), address: (v.address || "").trim(), power_mgr_target: (v.power_mgr_target || "").trim(),
          always_on: !!v.always_on, wake_on_hit_enabled: !!v.wake_on_hit_enabled, notes: v.notes || "",
        };
        if (editing) await api.put("/api/registry/nodes/" + encodeURIComponent(n.name), body, { suppressToast: true });
        else await api.post("/api/registry/nodes", body, { suppressToast: true });
        toast((editing ? "Saved " : "Added ") + body.name, "ok");
        if (o.onSaved) o.onSaved(body);
      },
    });
  }

  async function nodePower(node, action, refresh) {
    const svcs = node.service_slugs || [];
    if (action === "power_off" || action === "restart") {
      const off = action === "power_off";
      const ok = await confirm({
        title: (off ? "Power off " : "Restart ") + node.name + "?",
        body: off ? "Its guests are shut down first. Services on it go to sleep until it is woken." : "Its guests stop while it restarts and start again if they are set to.",
        affectedLabel: svcs.length ? "Services on it (" + svcs.length + ")" : null,
        affected: svcs.length ? svcs.slice(0, 12).concat(svcs.length > 12 ? ["and " + (svcs.length - 12) + " more"] : []) : null,
        typeName: node.name, action: off ? "Power off" : "Restart",
      });
      if (!ok) return;
    }
    try {
      await api.post("/api/infra/nodes/" + encodeURIComponent(node.name) + "/power", { action, force: false });
      toast({ wake: "Waking ", power_off: "Powering off ", restart: "Restarting " }[action] + node.name, "ok");
    } catch (_e) { return; }
    if (refresh) refresh();
  }

  async function deleteNode(node, refresh) {
    const svcs = node.service_slugs || [];
    if (svcs.length) {
      await confirm({ title: node.name + " still has services", body: "Move or delete its services first.", affected: svcs, action: "OK", danger: false });
      return;
    }
    if (!await confirm({ title: "Delete " + node.name + "?", body: "Only the orchestrator forgets it; nothing changes in Proxmox. Importing from Proxmox brings it back.", typeName: node.name, action: "Delete" })) return;
    await api.del("/api/registry/nodes/" + encodeURIComponent(node.name));
    toast("Deleted " + node.name, "ok");
    if (refresh) refresh();
  }

  // --- proxy hosts ---------------------------------------------------------------------------
  const FALLBACK_CATALOG = {
    entrypoints: [], cert_resolvers: [], cert_resolvers_known: false, default_entrypoints: [], default_cert_resolver: "",
    domains: [], redirect_available: null, hsts_available: null, config_files: [], config_file_template: "",
    default_config_file: "", source: "unavailable",
  };
  async function catalogFor(serviceSlug, hostname) {
    const q = new URLSearchParams({ hostname: hostname || "example.com" });
    if (serviceSlug) q.set("service", serviceSlug);
    try { return await api.get("/api/proxy/entrypoints?" + q, { suppressToast: true }); } catch (_e) { return FALLBACK_CATALOG; }
  }
  const routeName = (h) => "orchestrator-" + String(h || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

  // services: [{id, slug, name, ip, ip_source}]; host: a row of /api/proxy-hosts (edit) or none
  async function proxyHostDrawer(o) {
    const h = o.host; const editing = !!h;
    const services = o.services || [];
    const svcOf = (id) => services.find((s) => String(s.id) === String(id));
    const firstSvc = o.serviceId != null ? svcOf(o.serviceId) : null;
    const cat = await catalogFor(editing ? h.service_slug : firstSvc && firstSvc.slug, editing ? h.hostname : "");
    const ro = editing ? h.route_options : {};
    const wild = (ro.tls_main && cat.domains.includes(ro.tls_main) && (ro.tls_sans || []).length === 1 && ro.tls_sans[0] === "*." + ro.tls_main);
    const values = {
      hostname: editing ? h.hostname : "",
      service_id: editing ? String(h.service_id) : (firstSvc ? String(firstSvc.id) : (services[0] ? String(services[0].id) : "")),
      upstream: editing ? h.upstream : "",
      entry_points: editing && h.entry_points.length ? h.entry_points.slice() : (editing && h.traefik && h.traefik.entry_points ? h.traefik.entry_points.slice() : cat.default_entrypoints.slice()),
      cert_resolver: editing ? h.cert_resolver : (cat.default_cert_resolver || ""),
      cert_mode: !ro.tls_main ? "host" : wild ? "wild:" + ro.tls_main : "custom",
      tls_main: ro.tls_main || "", tls_sans: (ro.tls_sans || []).join("\n"),
      http_redirect: editing ? !!ro.http_redirect : cat.redirect_available !== false,
      pass_host_header: editing ? ro.pass_host_header !== false : true,
      hsts: editing ? !!ro.hsts : false,
      skip_upstream_tls_verify: editing ? !!ro.skip_upstream_tls_verify : false,
      config_file: editing ? (ro.config_file || "") : "",
      wake_on_hit: editing ? !!h.wake_on_hit : false,
    };
    const filesList = "dl-files-" + Math.random().toString(36).slice(2);
    // "Seen in: Traefik's static config, a route…" per resolver (cert_resolver_sources).
    const seenIn = (r) => ((cat.cert_resolver_sources || {})[r] || []).map((k) => (cat.cert_resolver_source_labels || {})[k] || k).join(", ");
    const resolverOptions = [{ value: "", label: "None (plain HTTP or a default certificate)" }]
      .concat(cat.cert_resolvers.map((r) => ({ value: r, label: seenIn(r) ? r + " · Seen in: " + seenIn(r) : r })));
    if (values.cert_resolver && !cat.cert_resolvers.includes(values.cert_resolver)) resolverOptions.push({ value: values.cert_resolver, label: values.cert_resolver + " (not known to Traefik)" });

    function entryPointsField(v, set) {
      if (!cat.entrypoints.length) {
        const inp = el("input", { class: "mono", placeholder: "websecure, web", value: (v.entry_points || []).join(", "), "aria-label": "Entry points" });
        inp.addEventListener("input", () => set("entry_points", inp.value.split(/[\s,]+/).filter(Boolean)));
        return el("div", {}, inp, el("div", { class: "help" }, "Traefik could not be read: type the entry point names."));
      }
      const box = el("div", { style: { display: "flex", flexWrap: "wrap", gap: "6px" }, role: "group", "aria-label": "Entry points" });
      for (const ep of cat.entrypoints) {
        const b = el("button", { type: "button", class: "chip", "aria-pressed": String(v.entry_points.includes(ep.name)), title: ep.address || null }, ep.name);
        b.addEventListener("click", () => {
          const on = v.entry_points.includes(ep.name);
          set("entry_points", on ? v.entry_points.filter((x) => x !== ep.name) : v.entry_points.concat([ep.name]));
          b.setAttribute("aria-pressed", String(!on));
          preview();
        });
        box.append(b);
      }
      return box;
    }
    function upstreamHelper(v, set) {
      const wrap = el("div", { class: "help" });
      const draw = () => {
        clearNode(wrap);
        const svc = svcOf(v.service_id);
        if (svc && svc.ip) {
          const host = svc.ip.includes(":") ? "[" + svc.ip + "]" : svc.ip;
          wrap.append("Its service's address is ", el("code", {}, svc.ip), svc.ip_source ? " (" + svc.ip_source + ")" : "", " ",
            el("button", { type: "button", class: "ghost small", style: { color: "var(--link)", height: "auto", padding: "0" }, onclick: () => {
              const cur = v.upstream || "";
              const m = cur.match(/^(\w+):\/\/[^/:]+(?::(\d+))?/);
              const next = (m ? m[1] : "http") + "://" + host + (m && m[2] ? ":" + m[2] : ":80");
              const inp = $one("#f-upstream"); inp.value = next; set("upstream", next); preview();
            } }, "Use it"));
        } else wrap.append("The address of the app, as Traefik reaches it.");
      };
      draw();
      wrap.redraw = draw;
      return wrap;
    }
    const previewBox = el("div", { class: "mono", style: { fontSize: "12px", color: "var(--text-2)", overflowWrap: "anywhere" } });
    function preview() {
      const v = d ? d.values : values;
      const parts = ["router " + (editing && h.extra && h.extra.traefik_manager_route_id ? h.extra.traefik_manager_route_id : routeName(v.hostname || "host")),
        "→ " + (v.upstream || "(no upstream: kept local only)"),
        "on " + ((v.entry_points || []).join(", ") || "(no entry point)"),
        "cert " + (v.cert_resolver || "none") + (v.cert_mode.startsWith("wild:") ? " (wildcard " + v.cert_mode.slice(5) + ")" : v.cert_mode === "custom" ? " (custom names)" : "")];
      if (v.http_redirect) parts.push("+ redirect");
      if (v.hsts) parts.push("+ HSTS");
      parts.push("in " + (v.config_file || cat.default_config_file || cat.config_file_template || "the default file"));
      previewBox.textContent = parts.join(" · ");
    }
    let d = null;
    const $one = (sel) => d.el.querySelector(sel);
    const clearNode = (n) => { while (n.firstChild) n.removeChild(n.firstChild); };
    let upHelp = null;
    d = drawer({
      title: editing ? "Edit " + h.hostname : "Add proxy host",
      sub: editing ? (h.enabled ? "" : "Off: Traefik does not serve it.") : "",
      values,
      saveLabel: editing ? "Save" : "Add proxy host",
      note: "Save " + (editing ? "updates" : "adds") + " it and pushes it to Traefik Manager.",
      fields: [
        { key: "hostname", label: "Hostname", required: true, mono: true, placeholder: "photos.example.lan" },
        { key: "service_id", label: "For service", type: "select", options: services.map((s) => ({ value: String(s.id), label: s.name + " (" + s.slug + ")" })), help: "Its state, wake on hit and maintenance page follow the service." },
        { key: "upstream", label: "Points to", mono: true, placeholder: "http://10.0.1.20:2283" },
        { type: "custom", key: "_up", render: (v, set) => (upHelp = upstreamHelper(v, set)) },
        { type: "section", label: "Serving" },
        { type: "custom", key: "entry_points", label: "Entry points", render: entryPointsField },
        cat.cert_resolvers_known
          ? { key: "cert_resolver", label: "Certificate resolver", type: "select", options: resolverOptions, help: "websecure (TLS) usually needs one; plain web does not." }
          : { key: "cert_resolver", label: "Certificate resolver", mono: true, placeholder: "blank = none", help: "Traefik could not be read, so the resolvers are not known: type its name (it is not known to Traefik until Traefik answers)." },
        { key: "cert_mode", label: "Certificate", type: "select", options: [{ value: "host", label: "One for this hostname" }].concat(cat.domains.map((x) => ({ value: "wild:" + x, label: "Wildcard: " + x + " + *." + x }))).concat([{ value: "custom", label: "Custom names…" }]) },
        { key: "tls_main", label: "Main name", mono: true, placeholder: "example.lan", show: (v) => v.cert_mode === "custom" },
        { key: "tls_sans", label: "Extra names", type: "textarea", rows: 2, placeholder: "*.example.lan", help: "One per line.", show: (v) => v.cert_mode === "custom" },
        { key: "http_redirect", label: "Redirect http:// to https://", type: "checkbox", help: cat.redirect_available === false ? "The redirect middleware does not exist in Traefik yet." : null },
        { key: "pass_host_header", label: "Send the visitor's hostname (Host header)", type: "checkbox", help: "Leave on: with it off, many apps redirect the browser to their raw LAN address." },
        { key: "hsts", label: "HSTS (browsers only use https afterwards)", type: "checkbox", help: cat.hsts_available === false ? "The HSTS middleware does not exist in Traefik yet." : "Browsers remember it for a long time: turn it on once https works for sure." },
        { key: "skip_upstream_tls_verify", label: "Don't verify the app's own certificate", type: "checkbox", help: "For an https:// app with a self-signed certificate (Proxmox, PBS).", show: (v) => /^https:/i.test(v.upstream || "") },
        { key: "wake_on_hit", label: "Wake on hit: a visit wakes its service", type: "checkbox" },
        { type: "section", label: "Advanced" },
        { key: "config_file", label: "Config file", mono: true, list: filesList, placeholder: cat.default_config_file || cat.config_file_template || "(the default)", help: "The Traefik Manager file a new route goes into. Blank: " + (cat.default_config_file || cat.config_file_template || "the default") + "." },
        { type: "custom", key: "_preview", label: "What Traefik Manager will get", render: () => el("div", { class: "card", style: { padding: "10px 12px", margin: 0, background: "var(--inset)" } }, previewBox, el("div", { class: "help", style: { marginTop: "6px" } }, "Checked after saving: the route must be loaded and answer.")) },
      ],
      async onSave(v) {
        let tls = { tls_main: "", tls_sans: [] };
        if (v.cert_mode.startsWith("wild:")) { const x = v.cert_mode.slice(5); tls = { tls_main: x, tls_sans: ["*." + x] }; }
        else if (v.cert_mode === "custom") tls = { tls_main: (v.tls_main || "").trim(), tls_sans: (v.tls_sans || "").split(/[\n,]+/).map((x) => x.trim()).filter(Boolean) };
        const body = {
          hostname: v.hostname.trim(), upstream: (v.upstream || "").trim(), service_id: Number(v.service_id),
          router_provider: editing ? h.router_provider : "traefik", extra: editing ? h.extra : {},
          wake_on_hit: !!v.wake_on_hit, entry_points: v.entry_points || [], cert_resolver: v.cert_resolver || "",
          route_options: Object.assign({
            pass_host_header: !!v.pass_host_header, skip_upstream_tls_verify: !!v.skip_upstream_tls_verify && /^https:/i.test(v.upstream || ""),
            http_redirect: !!v.http_redirect, hsts: !!v.hsts, config_file: (v.config_file || "").trim(),
          }, tls),
        };
        const out = editing
          ? await api.put("/api/proxy-hosts/" + h.id, body, { suppressToast: true })
          : await api.post("/api/proxy-hosts", body, { suppressToast: true });
        toast((editing ? "Saved " : "Added ") + body.hostname + (body.upstream ? "; pushing to Traefik Manager" : ""), "ok");
        for (const w of (out && out.warnings) || []) toast(w, "error");
        if (o.onSaved) o.onSaved(out);
      },
    });
    d.el.append(el("datalist", { id: filesList }, (cat.config_files || []).map((f) => el("option", { value: f }))));
    d.el.addEventListener("input", preview);
    d.el.addEventListener("change", () => { preview(); if (upHelp && upHelp.redraw) upHelp.redraw(); });
    preview();
    return d;
  }

  async function deleteProxyHosts(hosts, refresh) {
    const one = hosts.length === 1;
    const ok = await confirm({
      title: one ? "Delete " + hosts[0].hostname + "?" : "Delete " + hosts.length + " proxy hosts?",
      body: "Their routes are removed from Traefik Manager on the next push; the hosts go to the Archive for 30 days.",
      affected: one ? null : hosts.map((x) => x.hostname),
      typeName: one ? hosts[0].hostname : String(hosts.length),
      action: "Delete",
    });
    if (!ok) return false;
    if (one) {
      await api.del("/api/proxy-hosts/" + hosts[0].id);
      toast("Deleted " + hosts[0].hostname, "ok");
    } else await bulk("proxy-hosts", "delete", hosts.map((x) => x.id));
    if (refresh) refresh();
    return true;
  }

  // --- activity -------------------------------------------------------------------------------
  // One line per event of GET /api/activity: {cls, tag, text, href} - the service History tab,
  // the dashboard and the Activity page all describe an event the same way.
  const RUN_CLS = { succeeded: "ok", failed: "fail", running: "pending", queued: "pending", skipped: "warn", cancelled: "off" };
  const SEV_CLS = { error: "fail", critical: "fail", warn: "warn", info: "ok" };
  function describeEvent(ev) {
    if (ev.type === "run") {
      const what = ev.kind.charAt(0).toUpperCase() + ev.kind.slice(1);
      return { cls: RUN_CLS[ev.status] || "off", tag: ev.kind, text: what + " " + (ev.status === "succeeded" ? "ok" : ev.status) +
        (ev.error ? ": " + ev.error : "") + (ev.actor && ev.actor !== "system" ? " · " + ev.actor : ""), service: ev.service };
    }
    if (ev.type === "alert") {
      return { cls: ev.suppressed ? "off" : SEV_CLS[ev.severity] || "off", tag: "alert", text: ev.title + (ev.suppressed ? " (not sent: " + (ev.suppressed_reason || "muted") + ")" : ""), href: ev.link, service: ev.service };
    }
    const action = String(ev.action || "").replace(/[._]/g, " ");
    const target = String(ev.target || "").replace(/^[a-z_]+:/, "");
    return { cls: ev.result === "ok" ? "pending" : "fail", tag: "change", text: (ev.actor || "someone") + " · " + action + (target ? " " + target : ""), service: ev.service };
  }
  function eventRow(ev, opts) {
    const d = describeEvent(ev);
    const when = el("time", { datetime: ev.at, title: window.OrchTime ? window.OrchTime.fmt(ev.at) : ev.at, class: "mono faint", style: { fontSize: "12px", whiteSpace: "nowrap" } }, window.UI.fmt.short(ev.at));
    return el("div", { class: "event-row" }, when, el("span", { class: "pill " + d.cls }, d.tag),
      el("span", { class: "event-text" }, d.text,
        opts && opts.service && d.service ? [" · ", el("a", { href: "/services/" + encodeURIComponent(d.service) }, d.service)] : null),
      d.href ? el("a", { href: d.href, class: "faint", style: { fontSize: "12px" } }, "open") : null);
  }

  // --- sub-services under a parent (Services list, Dashboard, Map) --------------------------------
  // subs: [{slug, name, cls, label, kind, containers, stack: {href}|null, depth}] (views._sub_brief)
  function subKind(x) {
    if (x.kind === "stack") return "stack" + (x.containers ? " · " + x.containers + " container" + (x.containers === 1 ? "" : "s") : "");
    return x.kind || "";
  }
  function subList(subs, opts) {
    const o = opts || {};
    return el("div", { class: "sub-list" + (o.compact ? " compact" : "") }, subs.map((x) => el("div", { class: "sub-item", style: x.depth > 1 ? { paddingLeft: (x.depth - 1) * 14 + "px" } : null },
      el("span", { class: "pill " + x.cls }, x.label),
      el("a", { href: "/services/" + encodeURIComponent(x.slug), class: "sub-name" }, x.name),
      el("span", { class: "faint" }, subKind(x)),
      x.stack ? el("a", { href: x.stack.href, class: "sub-stack" }, "Manage stack →") : el("span"))));
  }

  window.Objects = { subList, subKind, bulk, slugify, serviceDrawer, serviceActions, deleteService, nodeDrawer, nodePower, deleteNode, proxyHostDrawer, deleteProxyHosts, catalogFor, describeEvent, eventRow };
})();
