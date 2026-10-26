/* Data sources: pull what lives in Proxmox, Traefik Manager, Portainer and on the guests; import
 * what is new; format routes. Everything comes from /api/sources/*. Built with textContent only -
 * nothing a remote system returns is ever put in as HTML. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, props, ...kids) => {
    const n = document.createElement(tag);
    if (props) for (const [k, v] of Object.entries(props)) { if (k === "className") n.className = v; else if (k === "text") n.textContent = v; else n[k] = v; }
    for (const k of kids) if (k !== null && k !== undefined && k !== false) n.append(k.nodeType ? k : document.createTextNode(String(k)));
    return n;
  };
  const code = (t) => el("code", null, t);
  const chip = (t, kind) => el("span", { className: "chip" + (kind ? " " + kind : "") }, t);
  const err = (e) => (e && e.message) || String(e);
  const api = (method, url, body) => apiFetch(method, url, body, { suppressToast: true });
  const when = (iso) => (iso ? fmtTime(iso) : "never");
  const msg = (id, text, kind) => { const m = $(id); m.textContent = text; m.className = "msg" + (kind ? " " + kind : ""); };
  const emptyRow = (cols, text) => el("tr", null, el("td", { colSpan: cols, className: "muted" }, text));
  const stateBadge = (s) => el("span", { className: "badge " + (s === "on" || s === "running" ? "healthy" : s === "off" || s === "stopped" ? "failed" : "unknown") }, s || "?");
  async function busyButton(btn, fn) {
    btn.disabled = true;
    try { return await fn(); } finally { btn.disabled = false; }
  }

  // -- tabs ---------------------------------------------------------------------------------------
  const TABS = ["proxmox", "ips", "traefik", "portainer"];
  const loaders = {};
  const loaded = new Set();
  function show(name, force) {
    if (!TABS.includes(name)) name = "proxmox";
    for (const t of TABS) {
      $("tab-" + t).hidden = t !== name;
      const b = document.querySelector(`.tab[data-tab="${t}"]`);
      b.setAttribute("aria-selected", String(t === name));
    }
    if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
    if (force || !loaded.has(name)) { loaded.add(name); loaders[name](); }
  }
  for (const b of document.querySelectorAll(".tab")) b.addEventListener("click", () => show(b.dataset.tab));
  window.addEventListener("hashchange", () => show(location.hash.slice(1)));

  async function loadOverview() {
    try {
      const o = await api("GET", "/api/sources/overview");
      const r = o.refreshed || {};
      $("m-proxmox").textContent = `${o.proxmox.nodes} nodes, ${o.proxmox.guests} guests` + (r.proxmox ? ` · ${when(r.proxmox)}` : "");
      $("m-ips").textContent = `${o.ips.missing} without an address` + (r.ips ? ` · ${when(r.ips)}` : "");
      $("m-traefik").textContent = `${o.traefik.hosts} hosts` + (o.traefik.never_pushed ? `, ${o.traefik.never_pushed} not pushed` : "") + (r.traefik ? ` · ${when(r.traefik)}` : "");
      $("m-portainer").textContent = o.portainer.configured ? (r.portainer ? when(r.portainer) : "") : "not set up";
    } catch (e) { /* the tabs report their own errors */ }
  }

  // -- Proxmox -----------------------------------------------------------------------------------------
  loaders.proxmox = async function () {
    let d;
    try { d = await api("GET", "/api/sources/proxmox"); }
    catch (e) { msg("px-msg", err(e), "error"); return; }
    if (d.proxmox_error) msg("px-msg", "Proxmox could not be read: " + d.proxmox_error, "error");
    $("px-nodes").replaceChildren(...(d.nodes.length ? d.nodes.map((n) => {
      const imp = el("button", { className: "secondary small" }, "Import");
      imp.addEventListener("click", () => busyButton(imp, async () => {
        try { await api("POST", "/api/sources/proxmox/nodes/import", { name: n.name }); toast("Imported " + n.name, "ok"); loaders.proxmox(); loadOverview(); }
        catch (e) { msg("px-msg", err(e), "error"); }
      }));
      return el("tr", null,
        el("td", null, n.name, n.registered === false ? chip("not imported", "warn") : null),
        el("td", null, stateBadge(n.state), n.health && n.health.checked && !n.health.reachable ? chip("API silent", "bad") : null),
        el("td", null, n.address || "—"), el("td", null, n.power_target || "—"), el("td", null, n.always_on ? "yes" : "no"),
        el("td", null, n.registered === false ? imp : el("a", { href: "/" }, "dashboard")));
    }) : [emptyRow(6, "No nodes.")]));
    $("px-guests").replaceChildren(...(d.guests.length ? d.guests.map((g) => {
      const imp = el("button", { className: "secondary small" }, "Import as service");
      imp.addEventListener("click", () => busyButton(imp, async () => {
        try { const r = await api("POST", "/api/sources/proxmox/guests/import", { node: g.node, vmid: g.vmid }); toast("Registered " + r.slug, "ok"); loaders.proxmox(); loadOverview(); }
        catch (e) { msg("px-msg", err(e), "error"); }
      }));
      return el("tr", null, el("td", null, g.node), el("td", null, g.vmid), el("td", null, g.kind), el("td", null, g.name),
        el("td", null, stateBadge(g.status)),
        el("td", null, g.service_slug ? el("a", { href: "/services/" + encodeURIComponent(g.service_slug) }, g.service_slug) : el("span", { className: "muted" }, "not registered")),
        el("td", null, g.service_slug ? "" : imp));
    }) : [emptyRow(7, d.guests_error ? "Guests could not be read: " + d.guests_error : "No guests.")]));
  };
  $("px-refresh").addEventListener("click", () => busyButton($("px-refresh"), async () => {
    msg("px-msg", "Pulling from Proxmox…");
    try {
      const r = await api("POST", "/api/sources/proxmox/refresh");
      const filled = Object.entries(r.addresses.filled).map(([n, a]) => `${n}=${a}`);
      const missing = r.guests.missing.length ? ` Not found in Proxmox: ${r.guests.missing.join(", ")}.` : "";
      const extra = r.guests.unregistered_guests.length ? ` ${r.guests.unregistered_guests.length} guest(s) not registered.` : "";
      msg("px-msg", (filled.length ? `Filled ${filled.join(", ")}. ` : "") + `Re-read ${r.guests.scanned} service(s).${missing}${extra}`, r.guests.missing.length ? "error" : "ok");
      loaders.proxmox(); loadOverview();
    } catch (e) { msg("px-msg", err(e), "error"); }
  }));

  // -- Addresses -----------------------------------------------------------------------------------------
  loaders.ips = async function () {
    let rows;
    try { rows = await api("GET", "/api/sources/ips"); }
    catch (e) { msg("ip-msg", err(e), "error"); return; }
    $("ip-rows").replaceChildren(...(rows.length ? rows.map((r) => el("tr", null,
      el("td", null, el("a", { href: "/services/" + encodeURIComponent(r.slug) }, r.slug)), el("td", null, r.node || "—"), el("td", null, r.guest),
      el("td", null, r.effective_ip || el("span", { className: "muted" }, "none")), el("td", null, r.effective_source_label || "—"),
      el("td", null, r.manual_ip || "—"), el("td", null, r.discovered_ip ? r.discovered_ip + " · " + when(r.ip_discovered_at) : "—"),
      el("td", { className: r.ip_discovery_error ? "st-error" : "" }, r.ip_discovery_error || ""))) : [emptyRow(8, "No service has a guest.")]));
  };
  async function discover(onlyMissing) {
    msg("ip-msg", "Discovering…");
    try {
      const r = await api("POST", "/api/sources/ips/discover", { only_missing: onlyMissing });
      msg("ip-msg", `Looked at ${r.scanned}, found an address for ${r.found}.`, "ok");
      loaders.ips(); loadOverview();
    } catch (e) { msg("ip-msg", err(e), "error"); }
  }
  $("ip-all").addEventListener("click", () => busyButton($("ip-all"), () => discover(false)));
  $("ip-missing").addEventListener("click", () => busyButton($("ip-missing"), () => discover(true)));

  // -- Traefik Manager -------------------------------------------------------------------------------------
  let serviceChoices = null;
  async function services() {
    if (!serviceChoices) serviceChoices = await api("GET", "/api/registry/services");
    return serviceChoices;
  }
  loaders.traefik = async function () {
    let d;
    try { d = await api("GET", "/api/sources/traefik"); }
    catch (e) { msg("tr-msg", err(e), "error"); return; }
    $("tr-unreadable").hidden = d.readable;
    const svcs = await services();
    $("tr-routes").replaceChildren(...(d.routes.length ? d.routes.map((r) => routeRow(r, svcs)) : [emptyRow(8, d.readable ? "Traefik Manager has no routes." : "Not readable.")]));
    $("tr-noroute-box").hidden = !d.no_route.length;
    $("tr-noroute").replaceChildren(...d.no_route.map((h) => el("div", null, code(h.hostname), " ",
      h.service ? el("a", { href: "/services/" + encodeURIComponent(h.service) }, h.service) : null, " ",
      h.enabled ? (h.never_pushed ? chip("never pushed", "warn") : chip("route gone", "bad")) : chip("switched off", null))));
  };
  function routeRow(r, svcs) {
    const shape = r.redirect_companion ? chip("redirect router", "ok") : r.owned ? chip("standard", "ok") : chip("hand-written / migrated", "warn");
    let record;
    if (r.host_id) record = r.service ? el("a", { href: "/services/" + encodeURIComponent(r.service) }, r.service) : el("span", { className: "muted" }, "record");
    else if (r.redirect_companion) record = el("span", { className: "muted" }, "—");
    else {
      const sel = el("select", null, new Option("— service —", ""), ...svcs.map((s) => new Option(`${s.name} (${s.slug})`, s.slug)));
      const add = el("button", { className: "small" }, "Add");
      add.addEventListener("click", async () => {
        const s = svcs.find((x) => x.slug === sel.value);
        if (!s) { msg("tr-msg", "Pick a service for " + r.hostnames[0] + " first.", "error"); return; }
        const full = await api("GET", "/api/registry/services/" + encodeURIComponent(s.slug));
        try {
          await api("POST", "/api/proxy-hosts", { hostname: r.hostnames[0], upstream: r.upstream || "", service_id: full.id, entry_points: r.entry_points || [], adopt: true });
          msg("tr-msg", "Recorded " + r.hostnames[0] + ".", "ok"); loaders.traefik(); loadOverview();
        } catch (e) { msg("tr-msg", err(e), "error"); }
      });
      record = el("span", null, sel, " ", add);
    }
    return el("tr", null, el("td", null, code(r.route)), el("td", null, r.hostnames.join(", ") || "—"), el("td", null, r.upstream || "—"),
      el("td", null, r.file || "—"), el("td", null, shape), el("td", null, r.enabled ? "on" : "parked"), el("td", null, record), el("td", null, ""));
  }
  $("tr-import").addEventListener("click", () => busyButton($("tr-import"), async () => {
    msg("tr-msg", "Importing…");
    try {
      const b = await api("POST", "/api/sources/traefik/import");
      msg("tr-msg", `Updated ${b.updated_count}, found ${b.discovered_count} route(s) with no record.`, "ok");
      loaders.traefik(); loadOverview();
    } catch (e) { msg("tr-msg", err(e), "error"); }
  }));

  // -- format ------------------------------------------------------------------------------------------------
  let plan = null;
  $("tr-plan").addEventListener("click", () => busyButton($("tr-plan"), planFormat));
  async function planFormat() {
    msg("fmt-msg", "Reading every route…");
    $("fmt").hidden = false;
    try { plan = await api("POST", "/api/sources/traefik/format/plan", hstsChoice() === null ? {} : { hsts: hstsChoice() }); }
    catch (e) { msg("fmt-msg", err(e), "error"); return; }
    if (plan.problem) { msg("fmt-msg", plan.problem, "error"); $("fmt-items").replaceChildren(); return; }
    const c = plan.counts;
    msg("fmt-msg", `${c.ready || 0} ready, ${c.standard || 0} already standard, ${c.blocked || 0} left alone, ${c.skipped || 0} skipped.`, "");
    $("fmt-every").disabled = !c.ready;
    $("fmt-every").textContent = c.ready ? `Format all ${c.ready} ready route(s)` : "Format all ready routes";
    const h = plan.hsts || {};
    $("fmt-hsts-note").textContent = h.available === false && hstsChoice() === true
      ? `The "${h.middleware}" middleware was not found in Traefik, so it is not added (create it, or set TRAEFIK_HSTS_MIDDLEWARE).` : "";
    $("fmt-items").replaceChildren(...plan.items.map(itemCard));
    $("fmt").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function kv(label, value) { return el("div", null, el("dt", null, label), el("dd", null, value)); }
  function itemCard(i) {
    const card = el("div", { className: "fmt-item " + i.status });
    const head = el("h4", null);
    if (i.status === "ready") { const cb = el("input", { type: "checkbox", checked: true }); cb.dataset.host = i.hostname; head.append(cb, " "); }
    head.append(code(i.hostname), " ", i.service ? el("span", { className: "muted" }, i.service + " · ") : "", chip(i.status === "ready" ? i.action : i.status, i.status === "ready" ? "ok" : i.status === "blocked" ? "bad" : null));
    card.append(head);
    if (i.reasons.length) card.append(el("div", { className: "why" }, i.reasons.join(" · ")));
    if (i.status === "ready") {
      card.append(el("ul", null, ...i.changes.map((c) => el("li", null, c))));
      const cur = i.current, tgt = i.target;
      card.append(el("dl", { className: "kv" },
        kv("Now", `${cur.name} in ${cur.file || "(default file)"} · ${cur.entry_points.join(", ") || "-"} · ${cur.cert_resolver || "no resolver"} · redirect router: ${cur.redirect_router ? "yes" : "no"}`),
        kv("Becomes", `${tgt.name} in ${tgt.file || "(default file)"} · ${tgt.entry_points.join(", ")} · ${tgt.cert_resolver || "no resolver"} · redirect router: ${tgt.options.http_redirect ? "yes" : "no"}`)));
      if (cur.middlewares.length) {
        const row = el("div", { className: "checks" }, el("span", { className: "muted" }, "Middlewares kept: "));
        for (const m of cur.middlewares) {
          const dropped = i.dropped_middlewares.includes(m);
          const cb = el("input", { type: "checkbox", checked: !dropped }); cb.dataset.host = i.hostname; cb.dataset.mw = m;
          row.append(el("label", { className: "inline", title: dropped ? "an old redirect-to-HTTPS middleware: the redirect router replaces it" : "" }, cb, " " + m + (dropped ? " (old redirect)" : "") + "  "));
        }
        card.append(row);
      }
    }
    for (const w of i.warnings) card.append(el("div", { className: "msg error" }, w));
    return card;
  }
  const hostBoxes = () => [...document.querySelectorAll("#fmt-items input[type=checkbox][data-host]:not([data-mw])")];
  // null = keep what each route has
  function hstsChoice() { const v = $("fmt-hsts").value; return v === "on" ? true : v === "off" ? false : null; }
  $("fmt-hsts").addEventListener("change", () => busyButton($("tr-plan"), planFormat));
  $("fmt-all").addEventListener("click", () => hostBoxes().forEach((b) => { b.checked = true; }));
  $("fmt-none").addEventListener("click", () => hostBoxes().forEach((b) => { b.checked = false; }));
  async function runFormat(chosen, button) {
    if (!plan) return;
    if (!chosen.length) { msg("fmt-msg", "Select at least one host.", "error"); return; }
    const hs = hstsChoice();
    const hsNote = hs === true ? " HSTS is added to each." : hs === false ? " HSTS is taken off each." : "";
    if (!confirm(`Rewrite ${chosen.length} route(s) now? Each is created beside the old one, checked, then the old one is removed; a failure puts it back and stops the run.${hsNote}`)) return;
    const items = chosen.map((h) => {
      const it = plan.items.find((x) => x.hostname === h);
      const mws = [...document.querySelectorAll(`#fmt-items input[data-mw][data-host="${CSS.escape(h)}"]`)];
      return { hostname: h, fingerprint: it.fingerprint, ...(mws.length ? { middlewares: mws.filter((b) => b.checked).map((b) => b.dataset.mw) } : {}) };
    });
    msg("fmt-msg", "Starting…");
    try {
      const r = await api("POST", "/api/sources/traefik/format/run", {
        items, stop_on_failure: $("fmt-stop").checked, allow_unverified: $("fmt-unverified").checked,
        ...(hs === null ? {} : { hsts: hs }),
      });
      await follow(r.run_id);
    } catch (e) { msg("fmt-msg", err(e), "error"); }
  }
  $("fmt-run").addEventListener("click", () => busyButton($("fmt-run"),
    () => runFormat(hostBoxes().filter((b) => b.checked).map((b) => b.dataset.host))));
  $("fmt-every").addEventListener("click", () => busyButton($("fmt-every"), () => {
    hostBoxes().forEach((b) => { b.checked = true; });
    return runFormat(hostBoxes().map((b) => b.dataset.host));
  }));
  async function follow(runId) {
    const ol = $("fmt-steps");
    ol.hidden = false;
    for (let n = 0; n < 3600; n++) {
      const run = await api("GET", "/api/runs/" + runId);
      ol.replaceChildren(...run.steps.map((s) => {
        const d = s.detail || {};
        const note = d.error || d.reason || d.message || d.to || d.ref || d.state || "";
        const li = el("li", { className: s.status === "failed" ? "failed" : s.status === "skipped" ? "muted" : "ok" }, s.name + (note ? " — " + note : ""));
        return li;
      }));
      if (run.status !== "running") {
        msg("fmt-msg", run.status === "succeeded" ? "Done." : "Finished with problems: " + (run.error || run.status), run.status === "succeeded" ? "ok" : "error");
        loaders.traefik(); loadOverview(); planFormat();
        return;
      }
      await new Promise((res) => setTimeout(res, 1000));
    }
  }

  // -- Portainer -----------------------------------------------------------------------------------------------
  loaders.portainer = async function () {
    let d;
    try { d = await api("GET", "/api/sources/portainer"); }
    catch (e) { msg("pt-msg", err(e), "error"); return; }
    $("pt-off").hidden = d.configured;
    if (!d.configured) { $("pt-rows").replaceChildren(emptyRow(6, "Portainer is not set up.")); return; }
    const bad = Object.entries(d.errors || {}).map(([k, v]) => `environment ${k} (${v})`);
    $("pt-errors").textContent = bad.length ? `${bad.length} environment(s) are unreachable, so they and their stacks are not shown: ${bad.join("; ")}` : "";
    $("pt-rows").replaceChildren(...(d.stacks.length ? d.stacks.map((s) => {
      const linkedTo = (s.linked_services || []);
      const adopt = el("button", { className: "secondary small" }, "Adopt…");
      adopt.addEventListener("click", () => openAdopt(s));
      return el("tr", null, el("td", null, el("strong", null, s.name)), el("td", null, String(s.endpoint_id)), el("td", null, stateBadge(s.status === "active" ? "running" : "stopped")),
        el("td", null, s.containers_total !== undefined ? `${s.containers_running}/${s.containers_total}` : "—"),
        el("td", null, ...(linkedTo.length ? linkedTo.flatMap((x, k) => [k ? ", " : "", el("a", { href: "/services/" + encodeURIComponent(x) }, x)]) : [el("span", { className: "muted" }, "not linked")])),
        el("td", null, linkedTo.length ? "" : adopt));
    }) : [emptyRow(6, "No stacks.")]));
  };
  $("pt-refresh").addEventListener("click", () => busyButton($("pt-refresh"), async () => { await loaders.portainer(); loadOverview(); }));

  let suggestion = null;
  const option = (value, text) => new Option(text, value);
  async function openAdopt(s) {
    $("adopt").hidden = false; $("a-result").hidden = true;
    msg("a-msg", "Reading what is there…");
    $("adopt").scrollIntoView({ behavior: "smooth", block: "nearest" });
    try { suggestion = await api("GET", `/api/stacks/suggest?endpoint=${s.endpoint_id}&name=${encodeURIComponent(s.name)}`); }
    catch (e) { msg("a-msg", err(e), "error"); return; }
    const g = suggestion;
    $("a-stack").textContent = g.stack;
    $("a-notes").textContent = g.notes.join(" ");
    $("a-slug").value = g.slug;
    $("a-parent").replaceChildren(option("", "(choose the service Portainer runs on)"), ...g.parents.map((p) => option(p.slug, `${p.name} (${p.guest})`)));
    $("a-parent").value = g.parent || "";
    $("a-group").value = "";
    $("a-children").replaceChildren(...g.children.map((c, i) => {
      const cb = el("input", { type: "checkbox", checked: true, id: "a-c-" + i });
      const slug = el("input", { type: "text", value: c.slug, id: "a-cs-" + i });
      const gate = el("select", { id: "a-cg-" + i }, option("ignore", "no"), option("required", "yes (a database)")); gate.value = c.backup_gate;
      return el("tr", null, el("td", null, cb), el("td", null, el("strong", null, c.container), " ", chip(c.image)), el("td", null, slug), el("td", null, gate));
    }));
    $("a-hosts").replaceChildren(...(g.hosts.length ? g.hosts.map((h, i) => {
      const cb = el("input", { type: "checkbox", id: "a-h-" + i, value: h.hostname, checked: !h.linked_to || h.name_matches });
      return el("label", { className: "inline" }, cb, ` ${h.hostname} → ${h.upstream}` + (h.linked_to ? ` (now on ${h.linked_to}: moves)` : ""));
    }) : [el("span", { className: "muted" }, "No proxy host points at a port this stack publishes.")]));
    $("a-deploy").disabled = !g.has_git; $("a-hook").disabled = !g.has_git;
    $("a-deploy").title = g.has_git ? "" : "Not deployed from Git in Portainer, so there is nothing to redeploy";
    msg("a-msg", g.already_linked.length ? "Already linked to " + g.already_linked.join(", ") : "", g.already_linked.length ? "error" : "");
    $("a-go").disabled = g.already_linked.length > 0;
  }
  $("a-cancel").addEventListener("click", () => { $("adopt").hidden = true; });
  $("a-go").addEventListener("click", async () => {
    const g = suggestion; if (!g) return;
    const children = g.children.map((c, i) => ({ c, i })).filter(({ i }) => $("a-c-" + i).checked)
      .map(({ c, i }) => ({ container: c.container, slug: $("a-cs-" + i).value.trim(), name: c.name, backup_gate: $("a-cg-" + i).value }));
    const hosts = g.hosts.map((h, i) => ({ h, i })).filter(({ i }) => $("a-h-" + i) && $("a-h-" + i).checked).map(({ h }) => h.hostname);
    const body = { endpoint_id: g.endpoint_id, stack: g.stack, slug: $("a-slug").value.trim(), parent: $("a-parent").value,
      group: $("a-group").value.trim(), children, hosts, probes: $("a-probes").checked,
      deploy: $("a-deploy").value || null, webhook: $("a-deploy").value && $("a-hook").value ? $("a-hook").value : null };
    msg("a-msg", "Creating…");
    try {
      const r = await api("POST", "/api/stacks/adopt", body);
      const lines = [`Linked: ${r.services.join(", ")}`, `Proxy hosts: ${r.hosts.join(", ") || "none"} (they reach Traefik Manager on the next push)`, `Probes added: ${r.probes}`];
      if (r.webhook_secret) lines.push("", "Webhook secret (copy it now; it is not shown again):", r.webhook_secret);
      lines.push("", "The registry has unsaved changes: Save to YAML to keep them.");
      $("a-result").textContent = lines.join("\n"); $("a-result").hidden = false;
      msg("a-msg", "Done.", "ok"); $("a-go").disabled = true;
      loaders.portainer(); loadOverview();
    } catch (e) { msg("a-msg", err(e), "error"); }
  });

  // -- start -----------------------------------------------------------------------------------------------------
  loadOverview();
  show(location.hash.slice(1));
})();
