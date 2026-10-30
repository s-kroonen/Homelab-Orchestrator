/* Proxy hosts (templates/proxy_hosts.html).
 * The rows paint at once from the database; GET /api/proxy-hosts then adds what Traefik and
 * Traefik Manager say about each (one read for the whole list) and is re-read on changes. */
(function () {
  "use strict";
  const { $, el, api, region, table } = window.UI;
  const { bulk, proxyHostDrawer, deleteProxyHosts } = window.Objects;
  const init = JSON.parse($("#initial").textContent);
  let services = init.services;
  let live = false; // rows carry Traefik's view
  const age = el("span", { class: "age" });

  // One status word for the Traefik column (docs/design/README.md: ok warn fail off pending).
  function traefikState(h) {
    if (!h.enabled) return { cls: "off", label: "off", sub: "switched off here" };
    if (!h.served) return { cls: "off", label: "off", sub: "its service is disabled" };
    if (!live) return { cls: "off", label: "…", sub: "reading Traefik" };
    if (h.sync_pending) return { cls: "pending", label: "not pushed", sub: (h.sync_changes || []).join(", ") };
    if (h.sync_state === "detached") return { cls: "warn", label: "changed in Traefik", sub: (h.sync_changes || []).join(", ") };
    const t = h.traefik;
    if (!t) return { cls: "off", label: h.router_provider, sub: "kept here only" };
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
    return tags.length ? el("span", { class: "tags" }, tags.map((t) => el("span", { class: "tag" }, t))) : "";
  }
  async function setEnabled(h, on) {
    try {
      await api.post("/api/proxy-hosts/" + h.id + "/enabled", { enabled: on }, { suppressToast: true });
      window.toast((on ? "Switched on " : "Switched off ") + h.hostname, "ok");
    } catch (e) { window.toast(e.message, "error"); }
    refresh();
  }
  const edit = (h) => proxyHostDrawer({ host: h, services, onSaved: refresh });
  // Push one or many hosts to Traefik Manager in one request; a failing host never stops the rest.
  async function push(hosts, verb) {
    hosts = [].concat(hosts);
    const what = hosts.length === 1 ? hosts[0].hostname : hosts.length + " proxy hosts";
    try {
      const r = await api.post("/api/push", { hostnames: hosts.map((h) => h.hostname) });
      const bad = (r.results || r.push_results || []).filter((x) => !x.ok);
      window.toast(bad.length ? bad.length + " not pushed: " + bad.slice(0, 3).map((x) => x.message).join("; ") + (bad.length > 3 ? "…" : "") : verb + " " + what, bad.length ? "error" : "ok");
    } catch (_e) { /* toast shown */ }
    t.clearSelection();
    refresh();
    if (window.OrchShell) window.OrchShell.refreshPending();
  }
  const pushAll = $("#push-all");
  const showPushAll = (hosts) => { const n = hosts.filter((h) => h.sync_pending).length; pushAll.hidden = !n; pushAll.textContent = "Push " + n + " not pushed"; };
  pushAll.addEventListener("click", () => push(t.rows.filter((h) => h.sync_pending), "Pushed"));

  const t = table($("#hosts"), {
    rows: init.hosts,
    key: (h) => h.id,
    name: (h) => h.hostname,
    noun: ["proxy host", "proxy hosts"],
    placeholder: "Filter by hostname or service",
    search: (h) => [h.hostname, h.service_slug, h.service_name, h.upstream].join(" "),
    sort: { key: "hostname", dir: 1 },
    age,
    columns: [
      { key: "hostname", label: "Hostname", sort: (h) => h.hostname.split(".").reverse().join("."),
        render: (h) => el("button", { type: "button", class: "ghost", style: { padding: 0, height: "auto", color: "var(--text)", font: "600 13.5px var(--mono)" }, onclick: () => edit(h) }, h.hostname) },
      { key: "service", label: "Service", sort: (h) => h.service_slug || "",
        render: (h) => h.service_slug ? el("a", { href: "/services/" + encodeURIComponent(h.service_slug) }, h.service_slug) : "—" },
      { key: "upstream", label: "Points to", sort: true, render: (h) => h.upstream ? el("span", { class: "mono" }, h.upstream) : el("span", { class: "faint" }, "nothing yet") },
      { key: "options", label: "Options", render: options },
      { key: "traefik", label: "Traefik", phone: false, sort: (h) => ["fail", "warn", "pending", "off", "ok"].indexOf(traefikState(h).cls),
        render: (h) => { const s = traefikState(h); return [el("span", { class: "pill " + s.cls, title: s.sub || null }, s.label), s.sub ? el("span", { class: "sub" }, s.sub) : null]; } },
      { key: "enabled", label: "On", phone: "On",
        render: (h) => { const cb = el("input", { type: "checkbox", class: "switch", role: "switch", "aria-label": (h.enabled ? "Switch off " : "Switch on ") + h.hostname }); cb.checked = h.enabled; cb.addEventListener("change", () => setEnabled(h, cb.checked)); return cb; } },
    ],
    chips: [
      { key: "pending", label: "Not pushed", test: (h) => h.sync_pending },
      { key: "detached", label: "Changed in Traefik", test: (h) => h.sync_state === "detached" },
      { key: "error", label: "Errors", test: (h) => traefikState(h).cls === "fail" || traefikState(h).label === "not loaded" },
      { key: "off", label: "Off", test: (h) => !h.enabled || !h.served },
      { key: "file", label: "In another file", test: (h) => h.config_file_misplaced },
    ],
    menu: (h) => [
      { label: "Edit…", onClick: () => edit(h) },
      h.sync_pending ? { label: "Push now", onClick: () => push(h, "Pushed") } : null,
      h.sync_state === "detached" ? { label: "Put back as configured here", onClick: () => push(h, "Put back") } : null,
      h.config_file_movable ? { label: "Move to " + h.config_file_resolved, onClick: async () => {
        try { await api.post("/api/proxy-hosts/" + h.id + "/move-config-file"); window.toast("Moved " + h.hostname + " to " + h.config_file_resolved, "ok"); } catch (_e) { /* toast */ }
        refresh();
      } } : null,
      { label: h.wake_on_hit ? "Turn wake on hit off" : "Turn wake on hit on", onClick: () => bulk("proxy-hosts", h.wake_on_hit ? "wake_on_hit_off" : "wake_on_hit_on", [h.id]).then(refresh, () => {}) },
      h.service_slug ? { label: "Open " + h.service_slug, href: "/services/" + encodeURIComponent(h.service_slug) } : null,
      "-",
      { label: "Delete…", danger: true, onClick: () => deleteProxyHosts([h], refresh).catch(() => {}) },
    ],
    bulk: (rows) => {
      const ids = rows.map((h) => h.id);
      const run = (action) => () => bulk("proxy-hosts", action, ids).then(() => { t.clearSelection(); refresh(); }, () => {});
      return [
        { label: "Push to Traefik Manager", onClick: () => push(rows, "Pushed") },
        { label: "Switch on", onClick: run("enable") },
        { label: "Switch off", onClick: run("disable") },
        { label: "Wake on hit on", onClick: run("wake_on_hit_on") },
        { label: "Wake on hit off", onClick: run("wake_on_hit_off") },
        { label: "Delete…", danger: true, onClick: () => deleteProxyHosts(rows, () => { t.clearSelection(); refresh(); }).catch(() => {}) },
      ];
    },
    empty: {
      title: "No proxy hosts yet",
      text: "Add one, or import the routes Traefik Manager already has.",
      action: el("a", { class: "btn small", href: "/sources#traefik" }, "Import from Traefik"),
    },
  });

  showPushAll(init.hosts);

  const r = region(el("div"), {
    load: () => api.get("/api/proxy-hosts", { suppressToast: true }),
    render: (hosts) => { live = true; t.setRows(hosts); showPushAll(hosts); $("#ph-count").textContent = hosts.length; },
    topics: ["registry", "pending"], wait: 1500, every: 60000, age,
  });
  const svcRegion = region(el("div"), {
    initial: services,
    load: () => api.get("/api/views/proxy-host-services", { suppressToast: true }),
    render: (d) => { services = d; },
    topics: ["registry"], wait: 3000,
  });
  function refresh() { r.refresh(); }

  $("#add-host").addEventListener("click", () => proxyHostDrawer({ services, onSaved: refresh }));
  const q = new URLSearchParams(location.search);
  if (q.get("new") === "1") proxyHostDrawer({ services, serviceId: q.get("service_id"), onSaved: refresh });
  if (q.get("edit")) {
    const id = Number(q.get("edit"));
    api.get("/api/proxy-hosts/" + id, { suppressToast: true }).then((h) => edit(h), () => window.toast("No proxy host " + id, "error"));
  }
  void svcRegion;
})();
