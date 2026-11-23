/* A proxy source's own tabs (source.js loads them through window.SourceViews), to the
 * SourceProxy wireframe. Needs objects.js and format_routes.js.
 *   proxy.hosts   this proxy's hosts: where each points, its options, what Traefik says, on/off;
 *                 add, edit, push, switch, delete (one or a selection).
 *   proxy.import  every route this Traefik Manager has next to its record; record a route that
 *                 has none; import (upstream, on/off) into the records.
 *   proxy.format  rewrite hand-written routes into the standard shape (format_routes.js).
 *   proxy.review  the orchestrator's review of this proxy (/api/pull/traefik/review), read only:
 *                 errors, what it cannot read yet, routes not in Traefik Manager's form, clean-up,
 *                 each with how to fix it, and what the orchestrator supports; Markdown / JSON.
 * Hosts come from /api/proxy-hosts?source=, routes from /api/pull/traefik?source=; a push goes
 * to the proxy that owns each host. Nothing from Traefik is put in as HTML. */
(function () {
  "use strict";
  const { el, fill, api, region, table, drawer } = window.UI;
  const { bulk, proxyHostDrawer, deleteProxyHosts } = window.Objects;
  const enc = encodeURIComponent;
  const toast = (m, k) => window.toast(m, k);
  window.SourceViews = window.SourceViews || {};

  // The services a proxy host can belong to (the drawer's list).
  let services = null;
  const loadServices = async () => (services = services || await api.get("/api/views/proxy-host-services", { suppressToast: true }));

  // --- Proxy hosts ------------------------------------------------------------------------------------
  window.SourceViews["proxy.hosts"] = function (panel, ctx) {
    const SRC = ctx.source.id;
    let live = false;
    const age = el("span", { class: "age" });
    const pushAll = el("button", { type: "button", class: "secondary small", hidden: true });
    const box = el("div");
    fill(panel, el("div", { style: { display: "flex", gap: "8px", justifyContent: "flex-end", marginBottom: "10px" } },
      pushAll, el("button", { type: "button", class: "small", onclick: add }, "+ Add proxy host")), box);

    function traefikState(h) {
      if (!h.enabled) return { cls: "off", label: "off", sub: "switched off here" };
      if (!h.served) return { cls: "off", label: "off", sub: "its service is disabled" };
      if (!live) return { cls: "off", label: "…", sub: "reading Traefik" };
      if (h.sync_pending) return { cls: "pending", label: "not pushed", sub: (h.sync_changes || []).join(", ") };
      if (h.sync_state === "detached") return { cls: "warn", label: "changed in Traefik", sub: (h.sync_changes || []).join(", ") };
      const t = h.traefik;
      if (!t) return { cls: "off", label: h.router_provider, sub: "kept here only" };
      if (t.state === "ok" && composite(h) && servers(h).length && downCount(h) === servers(h).length) return { cls: "fail", label: "no backend", sub: "every backend is DOWN" };
      if (t.state === "ok" && composite(h) && downCount(h)) return { cls: "warn", label: (servers(h).find((u) => u.status === "DOWN") || {}).role === "fallback" ? "fallback down" : "a backend down", sub: downCount(h) + " of " + servers(h).length + " DOWN" };
      if (t.state === "ok") return { cls: "ok", label: "serving", sub: h.config_file_misplaced ? "in " + (h.tm_config_file || "?") + ", not " + h.config_file_resolved : "" };
      if (t.state === "error") return { cls: "fail", label: "error", sub: t.message || "" };
      if (t.state === "not_loaded") return { cls: "warn", label: "not loaded", sub: t.message || "" };
      return { cls: "off", label: "unknown", sub: t.message || "" };
    }
    function options(h) {
      const o = h.route_options || {};
      const tags = [];
      if (h.cert_resolver) tags.push("HTTPS");
      if (o.http_redirect) tags.push("redirect");
      if (o.hsts) tags.push("HSTS");
      if (h.wake_on_hit) tags.push("wake on hit");
      if (o.tls_main) tags.push((o.tls_sans || []).some((x) => x.startsWith("*.")) ? "wildcard" : "own cert");
      if (o.pass_host_header === false) tags.push("own Host");
      return tags.length ? el("span", { class: "tags" }, tags.map((x) => el("span", { class: "tag" }, x))) : "";
    }
    // A host with several backends: a dot per server of its catch-all router (Traefik's UP/DOWN),
    // and an open row with every router and every backend (the ProxyHostsList wireframe).
    const expanded = new Set();
    const catchAll = (h) => { const m = h.service_model; return m && m.routers ? (m.routers.find((x) => !x.redirect && x.upstreams && x.upstreams.length) || null) : null; };
    const servers = (h) => { const r = catchAll(h); return r ? r.upstreams : []; };
    const composite = (h) => !!(h.service_model && ["failover", "weighted", "mirroring", "mixed"].includes(h.service_model.strategy)) || ((h.service_model && h.service_model.routers || []).filter((x) => !x.redirect).length > 1);
    const downCount = (h) => servers(h).filter((u) => u.status === "DOWN").length;
    function dots(h) {
      if (!composite(h)) return null;
      return el("span", { class: "be-dots", "aria-label": servers(h).map((u) => u.role + " " + (u.status || "unknown")).join(", ") },
        servers(h).map((u) => el("i", { class: "be-dot " + (u.status === "UP" ? "ok" : u.status === "DOWN" ? "fail" : "off"), title: u.role + ": " + u.url + " " + (u.status || "") })));
    }
    function hostDetail(h) {
      const m = h.service_model;
      if (!m || !expanded.has(h.id)) return null;
      const hc = (c) => c ? [c.path || "/", c.interval ? " · every " + c.interval : "", c.timeout ? " · " + c.timeout : "", c.hostname ? " · as " + c.hostname : ""].join("") : "no health check";
      const routers = el("section", { class: "be-card" }, el("div", { class: "sec-label" }, "Routers ", el("span", { class: "faint" }, String(m.routers.length))),
        m.routers.map((r) => el("div", { class: "be-line" },
          el("div", {}, el("span", { class: "mono" }, r.router.split("@")[0]), r.priority != null ? el("span", { class: "faint" }, " · priority " + r.priority) : null,
            el("div", { class: "faint mono be-rule" }, r.redirect ? "HTTP -> HTTPS" : r.rule)),
          el("span", { class: "tag" }, r.redirect ? "redirect" : r.strategy))),
        el("div", { class: "faint be-note" }, (h.tm_config_file ? "File " + h.tm_config_file + " · " : "") + (h.sync_state === "external" ? "read only here" + (h.read_only ? " (" + h.read_only + ")" : "") + ": edit it in Traefik Manager" : "editable here and in Traefik Manager")));
      const main = catchAll(h);
      const backends = el("section", { class: "be-card" }, el("div", { class: "sec-label" }, "Backends · " + (main ? main.strategy : m.strategy)),
        servers(h).map((u) => el("div", { class: "be-line" },
          el("div", {}, el("span", { class: "faint" }, u.role + " "), el("span", { class: "mono" }, u.url),
            el("div", { class: "faint be-rule" }, hc(u.health_check) + (u.pass_host_header === false ? " · its own Host header" : ""))),
          u.status ? el("span", { class: "pill " + (u.status === "UP" ? "ok" : "fail") }, u.status) : el("span", { class: "faint" }, "—"))),
        el("div", { class: "be-note" },
          downCount(h) && downCount(h) === servers(h).length ? el("span", { class: "faint" }, "No backend answers: visitors see the maintenance page if the route has one. ") : null,
          el("button", { type: "button", class: "ghost small", style: { color: "var(--link)", padding: 0 }, onclick: () => window.Objects.backendsDrawer(h) }, "Backends…"),
          ["failover", "weighted", "mirroring", "mixed"].includes(m.strategy) ? [" · ", el("button", { type: "button", class: "ghost small", style: { color: "var(--link)", padding: 0 }, onclick: () => window.Objects.editBackends(h, () => r.refresh()) }, "Change backends…")] : null));
      return el("div", { class: "be-detail" }, routers, backends);
    }
    function toggle(h) { if (expanded.has(h.id)) expanded.delete(h.id); else expanded.add(h.id); t.render(); }
    async function setEnabled(h, on) {
      try { await api.post("/api/proxy-hosts/" + h.id + "/enabled", { enabled: on }, { suppressToast: true }); toast((on ? "Switched on " : "Switched off ") + h.hostname, "ok"); }
      catch (e) { toast(e.message, "error"); }
      r.refresh();
    }
    async function edit(h) { proxyHostDrawer({ host: h, services: await loadServices(), onSaved: () => r.refresh() }); }
    async function add() { proxyHostDrawer({ services: await loadServices(), source: SRC, onSaved: () => r.refresh() }); }
    const linkHost = (h) => window.Link.proxyHost({ hostname: h.hostname, source: SRC, sourceName: ctx.source.name, service_slug: h.service_slug, onDone: () => { services = null; r.refresh(); } });
    async function push(hosts, verb) {
      hosts = [].concat(hosts);
      const what = hosts.length === 1 ? hosts[0].hostname : hosts.length + " proxy hosts";
      try {
        const res = await api.post("/api/push", { hostnames: hosts.map((h) => h.hostname) });
        const bad = (res.results || []).filter((x) => !x.ok);
        toast(bad.length ? bad.length + " not pushed: " + bad.slice(0, 3).map((x) => x.message).join("; ") + (bad.length > 3 ? "…" : "") : verb + " " + what, bad.length ? "error" : "ok");
      } catch (_e) { /* toast shown */ }
      t.clearSelection(); r.refresh();
      if (window.OrchShell) window.OrchShell.refreshPending();
    }
    pushAll.addEventListener("click", () => push(t.rows.filter((h) => h.sync_pending), "Pushed"));

    const t = table(box, {
      // ?q= filters it (a link to one host lands here with its name)
      rows: [], key: (h) => h.id, name: (h) => h.hostname, noun: ["proxy host", "proxy hosts"], urlState: true,
      placeholder: "Filter by hostname or service", age,
      search: (h) => [h.hostname, h.service_slug, h.service_name, h.upstream, h.service_model ? h.service_model.display : ""].join(" "),
      sort: { key: "hostname", dir: 1 },
      detail: hostDetail,
      columns: [
        { key: "hostname", label: "Hostname", sort: (h) => h.hostname.split(".").reverse().join("."),
          render: (h) => el("span", { class: "host-cell" }, h.service_model && h.service_model.routers && h.service_model.routers.length
            ? el("button", { type: "button", class: "ghost be-toggle", "aria-expanded": String(expanded.has(h.id)), "aria-label": (expanded.has(h.id) ? "Close " : "Open ") + "routers and backends of " + h.hostname, onclick: () => toggle(h) }, expanded.has(h.id) ? "▾" : "▸") : null,
            el("button", { type: "button", class: "ghost", style: { padding: 0, height: "auto", color: "var(--text)", font: "600 13.5px var(--mono)" }, onclick: () => edit(h) }, h.hostname)) },
        { key: "service", label: "Service", sort: (h) => h.service_slug || "~",
          render: (h) => h.service_slug ? el("a", { href: "/services/" + enc(h.service_slug) }, h.service_slug)
            : [el("span", { class: "faint" }, "not linked"), " · ", el("button", { type: "button", class: "ghost small", style: { color: "var(--link)", padding: "0" }, onclick: () => linkHost(h) }, "Link…")] },
        { key: "upstream", label: "Points to", sort: true, render: (h) => [dots(h), window.Objects.pointsTo(h)] },
        { key: "options", label: "Options", render: options },
        { key: "traefik", label: "Traefik", phone: false, sort: (h) => ["fail", "warn", "pending", "off", "ok"].indexOf(traefikState(h).cls),
          render: (h) => { const s = traefikState(h); return [el("span", { class: "pill " + s.cls, title: s.sub || null }, s.label), s.sub ? el("span", { class: "sub" }, s.sub) : null]; } },
        { key: "enabled", label: "On", phone: "On",
          render: (h) => { const cb = el("input", { type: "checkbox", class: "switch", role: "switch", "aria-label": (h.enabled ? "Switch off " : "Switch on ") + h.hostname }); cb.checked = h.enabled; cb.addEventListener("change", () => setEnabled(h, cb.checked)); return cb; } },
      ],
      chips: [
        { key: "unlinked", label: "Not linked", test: (h) => !h.service_slug },
        { key: "pending", label: "Not pushed", test: (h) => h.sync_pending },
        { key: "detached", label: "Changed in Traefik", test: (h) => h.sync_state === "detached" },
        { key: "error", label: "Errors", test: (h) => traefikState(h).cls === "fail" || traefikState(h).label === "not loaded" },
        { key: "off", label: "Off", test: (h) => !h.enabled || !h.served },
        { key: "several", label: "Several backends", test: (h) => composite(h) },
        { key: "bedown", label: "A backend down", test: (h) => downCount(h) > 0 },
        { key: "handwritten", label: "Read only", test: (h) => h.sync_state === "external" },
        { key: "file", label: "In another file", test: (h) => h.config_file_misplaced },
      ],
      menu: (h) => [
        { label: "Edit…", onClick: () => edit(h) },
        h.adoptable ? { label: "Adopt the route that serves it", onClick: async () => {
          try { const a = await api.post("/api/proxy-hosts/" + h.id + "/adopt-route", undefined, { suppressToast: true }); toast("Adopted " + a.route + " for " + h.hostname, "ok"); }
          catch (e) { toast(e.message, "error"); }
          r.refresh();
        } } : null,
        h.service_model && h.service_model.routers.length ? { label: "Backends…", onClick: () => window.Objects.backendsDrawer(h) } : null,
        h.service_model && ["failover", "weighted", "mirroring", "mixed"].includes(h.service_model.strategy) ? { label: "Change backends…", onClick: () => window.Objects.editBackends(h, () => r.refresh()) } : null,
        h.sync_pending ? { label: "Push now", onClick: () => push(h, "Pushed") } : null,
        h.sync_state === "detached" ? { label: "Put back as configured here", onClick: () => push(h, "Put back") } : null,
        h.config_file_movable ? { label: "Move to " + h.config_file_resolved, onClick: async () => {
          try { await api.post("/api/proxy-hosts/" + h.id + "/move-config-file"); toast("Moved " + h.hostname + " to " + h.config_file_resolved, "ok"); } catch (_e) { /* toast */ }
          r.refresh();
        } } : null,
        { label: h.wake_on_hit ? "Turn wake on hit off" : "Turn wake on hit on", onClick: () => bulk("proxy-hosts", h.wake_on_hit ? "wake_on_hit_off" : "wake_on_hit_on", [h.id]).then(() => r.refresh(), () => {}) },
        h.service_slug ? { label: "Open " + h.service_slug, href: "/services/" + enc(h.service_slug) } : null,
        { label: h.service_slug ? "Link to another service…" : "Link to a service…", onClick: () => linkHost(h) },
        ctx.source.address ? { label: "Open in Traefik Manager ↗", href: ctx.source.address } : null,
        "-",
        { label: "Delete…", danger: true, onClick: () => deleteProxyHosts([h], () => r.refresh()).catch(() => {}) },
      ].filter(Boolean),
      bulk: (rows) => {
        const ids = rows.map((h) => h.id);
        const run = (action) => () => bulk("proxy-hosts", action, ids).then(() => { t.clearSelection(); r.refresh(); }, () => {});
        return [
          { label: "Push to Traefik Manager", onClick: () => push(rows, "Pushed") },
          { label: "Switch on", onClick: run("enable") },
          { label: "Switch off", onClick: run("disable") },
          { label: "Wake on hit on", onClick: run("wake_on_hit_on") },
          { label: "Wake on hit off", onClick: run("wake_on_hit_off") },
          { label: "Delete…", danger: true, onClick: () => deleteProxyHosts(rows, () => { t.clearSelection(); r.refresh(); }).catch(() => {}) },
        ];
      },
      empty: { title: "No proxy hosts yet", text: "Add one, or record the routes " + ctx.source.name + " already has.",
        action: el("button", { type: "button", class: "small", onclick: () => ctx.show && ctx.show("import") }, "Import") },
    });

    const r = region(el("div"), {
      load: () => api.get("/api/proxy-hosts?source=" + enc(SRC), { suppressToast: true }),
      render: (hosts) => {
        live = true; t.setRows(hosts); ctx.setCount(hosts.length);
        const n = hosts.filter((h) => h.sync_pending).length;
        pushAll.hidden = !n; pushAll.textContent = "Push " + n + " not pushed";
      },
      onError: (e) => toast(e.message, "error"),
      topics: ["registry", "pending"], wait: 1500, every: 60000, age,
    });
    // ?edit=<id> opens that host's drawer; ?new=1&service_id= the add drawer (/find/proxy-host)
    const p = new URLSearchParams(location.search);
    const editId = Number(p.get("edit"));
    if (editId) api.get("/api/proxy-hosts/" + editId, { suppressToast: true }).then(edit, () => toast("No proxy host " + editId, "error"));
    else if (p.get("new") === "1") loadServices().then((svcs) => proxyHostDrawer({ services: svcs, source: SRC, serviceId: p.get("service_id"), onSaved: () => r.refresh() }));
    for (const k of ["edit", "new", "service_id"]) p.delete(k);
    history.replaceState(history.state, "", location.pathname + (p.toString() ? "?" + p : ""));
  };

  // --- Import -------------------------------------------------------------------------------------------
  window.SourceViews["proxy.import"] = function (panel, ctx) {
    const SRC = ctx.source.id;
    const q = "?source=" + enc(SRC);
    const age = el("span", { class: "age" });
    const banner = el("div");
    const box = el("div");
    const noRoute = el("div");
    const importBtn = el("button", { type: "button", class: "small" }, "Import from Traefik Manager");
    fill(panel,
      el("div", { style: { display: "flex", gap: "10px", alignItems: "center", flexWrap: "wrap", marginBottom: "10px" } },
        el("span", { class: "faint", style: { fontSize: "13px", flex: "1", minWidth: "240px" } },
          "Every route " + ctx.source.name + " has. Import takes over the upstream and on/off of routes that already have a record; record a route that has none with Add."),
        importBtn),
      banner, box, noRoute);

    async function record(rt) {
      const svcs = await loadServices();
      drawer({
        title: "Record " + rt.hostnames[0],
        sub: "Adds the proxy host here for the route " + rt.route + " as it is; nothing in Traefik Manager changes.",
        values: { service_id: "" },
        saveLabel: "Add",
        fields: [{ key: "service_id", label: "Service", type: "select", required: true,
          options: [{ value: "", label: "— pick a service —" }].concat((svcs || []).map((s) => ({ value: String(s.id), label: s.name + " (" + s.slug + ")" }))) }],
        async onSave(v) {
          if (!v.service_id) throw new Error("Pick the service it belongs to.");
          await api.post("/api/proxy-hosts" + q, { hostname: rt.hostnames[0], upstream: rt.upstream || "", service_id: Number(v.service_id), entry_points: rt.entry_points || [], adopt: true }, { suppressToast: true });
          toast("Recorded " + rt.hostnames[0], "ok");
          view.refresh();
        },
      });
    }
    // Whether a host's record can rewrite it: its contents decide, not who made it or its name.
    const shape = (rt) => rt.redirect_companion ? el("span", { class: "pill off", title: rt.read_only || "" }, "redirect router")
      : rt.editable ? el("span", { class: "pill ok" }, "editable") : el("span", { class: "pill warn", title: rt.read_only || "" }, "read only");
    const t = table(box, {
      rows: [], key: (rt) => rt.route, name: (rt) => rt.route, noun: ["route", "routes"], urlState: false, age,
      placeholder: "Filter routes", search: (rt) => [rt.route, rt.hostnames.join(" "), rt.upstream, rt.file, rt.service].join(" "),
      sort: { key: "host", dir: 1 },
      columns: [
        { key: "host", label: "Host", sort: (rt) => (rt.hostnames[0] || "~"), render: (rt) => rt.hostnames.length ? el("strong", { class: "mono" }, rt.hostnames.join(", "))
          : el("span", { class: "mono faint", title: "Matched by its rule, not by a host name" }, rt.rule || "—") },
        { key: "route", label: "Route", sort: true, render: (rt) => el("span", { class: "mono faint", style: { fontSize: "12.5px" } }, rt.route) },
        { key: "upstream", label: "Points to", render: (rt) => rt.upstream ? el("span", { class: "mono" }, rt.upstream) : "—" },
        { key: "file", label: "File", phone: false, render: (rt) => rt.file ? el("span", { class: "mono", style: { fontSize: "12.5px" } }, rt.file) : "—" },
        { key: "shape", label: "Shape", render: shape },
        { key: "on", label: "On", render: (rt) => el("span", { class: "pill " + (rt.enabled ? "ok" : "off") }, rt.enabled ? "on" : "parked") },
        { key: "record", label: "Record", sort: (rt) => (rt.host_id ? 1 : 0),
          render: (rt) => rt.host_id ? (rt.service ? el("a", { href: "/services/" + enc(rt.service) }, rt.service) : el("span", { class: "faint" }, "recorded"))
            : rt.redirect_companion || !rt.hostnames.length ? el("span", { class: "faint" }, "—")
            : el("button", { type: "button", class: "secondary small", onclick: () => record(rt) }, "Add…") },
      ],
      chips: [
        { key: "new", label: "No record", test: (rt) => !rt.host_id && !rt.redirect_companion },
        { key: "hand", label: "Read only", test: (rt) => !rt.editable && !rt.redirect_companion },
        { key: "parked", label: "Parked", test: (rt) => !rt.enabled },
      ],
      empty: { title: "No routes", text: ctx.source.name + " has no routes, or cannot be read." },
    });
    const view = region(el("div"), {
      load: () => api.get("/api/pull/traefik" + q, { suppressToast: true }),
      render: (d) => {
        fill(banner, d.readable ? null : el("div", { class: "banner fail" }, "Traefik Manager is not configured or could not be read."));
        t.setRows(d.routes);
        ctx.setCount(d.routes.filter((rt) => !rt.host_id && !rt.redirect_companion).length);
        fill(noRoute, d.no_route.length ? el("section", { class: "card", style: { marginTop: "18px" } },
          el("div", { class: "card-head" }, el("h2", {}, "Recorded here, no route yet"), el("span", { class: "faint", style: { fontSize: "12.5px" } }, "a push creates them")),
          el("div", { class: "src-list", style: { gap: "6px", fontSize: "13px" } }, d.no_route.map((h) => el("div", {},
            el("span", { class: "mono" }, h.hostname), " ", h.service ? el("a", { href: "/services/" + enc(h.service) }, h.service) : null, " ",
            el("span", { class: "pill " + (h.enabled ? (h.never_pushed ? "pending" : "fail") : "off") }, h.enabled ? (h.never_pushed ? "never pushed" : "route gone") : "switched off"))))) : null);
      },
      onError: (e) => fill(banner, el("div", { class: "banner fail" }, e.message)),
      every: 60000, age,
    });
    importBtn.addEventListener("click", async () => {
      importBtn.disabled = true;
      try {
        const b = await api.post("/api/pull/traefik/import" + q, {}, { suppressToast: true });
        toast("Updated " + b.updated_count + ", found " + b.discovered_count + " route(s) with no record", "ok");
      } catch (e) { toast(e.message, "error"); }
      finally { importBtn.disabled = false; }
      view.refresh();
    });
  };

  // --- Review (ProxyReview wireframe) ---------------------------------------------------------------
  const GROUP_TONE = { error: "fail", unread: "warn", handwritten: "pending", cleanup: "off" };
  const SUPPORT_TONE = { edits: "ok", reads: "off", "not yet": "warn" };
  window.SourceViews["proxy.review"] = function (panel, ctx) {
    const SRC = ctx.source.id;
    const url = "/api/pull/traefik/review?source=" + enc(SRC);
    const head = el("div", { class: "findings-head" });
    const box = el("div");
    fill(panel, head, box);
    let fresh = false;
    const view = region(box, {
      load: () => { const u = url + (fresh ? "&fresh=true" : ""); fresh = false; return api.get(u, { suppressToast: true }); },
      render: (rep) => draw(rep),
      onError: (e) => fill(box, el("div", { class: "card" }, el("div", { class: "dt-empty" }, el("strong", {}, "Cannot read this proxy"), el("div", {}, e.message)))),
      every: 600000,
    });
    function headLine(rep) {
      const c = rep.counts;
      fill(head,
        el("div", { class: "findings-what" }, el("strong", {}, "Review"),
          el("span", { class: "faint" }, " · read " + window.UI.fmt.time(rep.read_at) + " · " + c.routers + " routers, " + c.middlewares + " middlewares, " + c.services + " services, " + c.transports + " transports, " + c.files + " files")),
        el("div", { class: "findings-tools" },
          el("button", { type: "button", class: "small", onclick: () => { fresh = true; view.refresh(); } }, "Read again"),
          el("a", { class: "btn secondary small", href: url + "&fmt=md", download: "" }, "Export Markdown"),
          el("a", { class: "btn secondary small", href: url + "&fmt=json", download: "" }, "Export JSON")));
    }
    function supportCard(rep) {
      return el("aside", { class: "card review-support" },
        el("div", { class: "card-head" }, el("h2", {}, "What the orchestrator supports")),
        el("div", { class: "review-sup" }, rep.support.map((x) => [el("span", {}, x.what), el("span", {}, el("span", { class: "pill " + (SUPPORT_TONE[x.status] || "off") }, x.status))])),
        el("div", { class: "faint review-sup-note" }, "Edits go through Traefik Manager's own forms, so its UI keeps working." +
          (rep.transports_known ? "" : " Servers transports are known by name only until the Traefik API connection is added.")));
    }
    function draw(rep) {
      headLine(rep);
      ctx.setCount && ctx.setCount(rep.counts.findings);
      const list = el("section", { class: "card findings" });
      for (const g of rep.groups) {
        list.append(el("div", { class: "findings-grp" }, el("span", { class: "pill " + (GROUP_TONE[g.key] || "off") }, g.label), el("span", { class: "faint" }, " " + g.findings.length)));
        for (const f of g.findings) {
          list.append(el("div", { class: "review-row" },
            el("span", { class: "mono" }, f.target),
            el("span", {}, f.detail),
            el("span", { class: "mono faint review-file" }, f.file || ""),
            el("span", { class: "faint review-fix" }, f.fix)));
        }
      }
      if (!rep.groups.length) list.append(el("div", { class: "dt-empty" }, el("strong", {}, "Nothing to report"), el("div", {}, "No errors, everything is read, every route is in Traefik Manager's form and nothing needs cleaning up.")));
      fill(box,
        el("div", { class: "banner info" }, "Read only: nothing here changes the proxy. Fix things in Traefik Manager, or hand the export to an agent; it carries the data and what the orchestrator supports."),
        el("div", { class: "review-grid" }, list, supportCard(rep)));
    }
  };

  // --- Format routes ------------------------------------------------------------------------------------
  window.SourceViews["proxy.format"] = function (panel, ctx) {
    const box = el("section", { class: "card" });
    fill(panel, box);
    window.FormatRoutes.mount(box, ctx.source.id);
  };
})();
