/* One service (templates/service_detail.html), built to the ServiceOverview / ServiceConfig /
 * ServiceRelated wireframes: header actions, four tabs in the URL, everything edited in drawers,
 * every section refreshed in place on live events.
 *
 * Data: GET /api/views/service/<slug> (the database: paints at once). Live readings come after,
 * each in its own section: the controllers (/controller?live=true), what is switched on
 * (/linked), the proxy hosts' push state, the backups, the deploys and the dumps. */
(function () {
  "use strict";
  const { $, el, clear, fill, append, api, fmt, region, table, drawer, confirm, menuButton, tabs } = window.UI;
  const O = window.Objects;
  const toast = (m, k) => window.toast(m, k);
  const enc = encodeURIComponent;

  let V = JSON.parse($("#initial").textContent);
  let S = V.service;
  const slug = S.slug;
  const base = "/api/services/" + enc(slug);
  const reg = "/api/registry/services/" + enc(slug);
  const ctl = () => S.controller || {};
  const hasStack = () => ctl().type === "portainer" && !!ctl().stack;
  const hasContainer = () => ctl().type === "portainer" && !!ctl().container;
  const isRoot = () => !S.parent;
  const hasGuest = () => S.guest_kind !== "none" && S.guest_id != null;
  const mine = () => new Set([S.id].concat((S.children || []).map((c) => c.id), (V.subs || []).map((c) => c.id)));
  const relevant = (d) => d.service_id == null || mine().has(d.service_id);

  let historyLoaded = false;
  let ctlInfo = null;   // /controller?live=true
  let linked = null;    // /linked
  let hostSync = {};    // proxy host id -> row of /api/proxy-hosts
  let cands = null;     // /proxy-host-candidates

  // --- tabs (old #probes / #backups / #deploys links still land in the right place) ----------
  const OLD_HASH = { probes: "configuration", "proxy-hosts": "related", backups: "related", stack: "related", onoff: "related", deploys: "related", dumps: "related", "health-strip": "overview" };
  const SPOT = { deploys: "stack-card", dumps: "dumps-card", backups: "backups-card", "proxy-hosts": "hosts", onoff: "onoff-card" };
  const hash = location.hash.slice(1);
  if (hash && OLD_HASH[hash] && !new URLSearchParams(location.search).get("tab")) {
    const u = new URL(location.href); u.searchParams.set("tab", OLD_HASH[hash]); u.hash = ""; history.replaceState(null, "", u);
  }
  const T = tabs($("#svc-tabs"), { onChange: (t) => { if (t === "history" && !historyLoaded) setTimeout(loadHistory, 0); } });
  if (SPOT[hash]) setTimeout(() => { const n = document.getElementById(SPOT[hash]); if (n && !n.hidden) n.scrollIntoView({ block: "start" }); }, 50);
  $("#to-history").addEventListener("click", (ev) => { ev.preventDefault(); T.show("history"); window.scrollTo({ top: 0 }); });

  // --- small helpers -----------------------------------------------------------------------------
  const facts = (pairs) => el("dl", { class: "facts" }, pairs.filter(Boolean).map(([k, v]) => [el("dt", {}, k), el("dd", {}, v == null || v === "" ? "—" : v)]));
  const VERDICT_CLS = { healthy: "ok", failed: "fail", down: "off", unknown: "off", skipped: "off" };
  const stepsList = (steps) => el("ol", { class: "steps" }, (steps || []).map((s) => {
    const d = s.detail || {};
    const note = d.error || d.reason || d.note || d.message || (d.size ? fmt.bytes(d.size) : "");
    return el("li", { class: s.status === "failed" ? "failed" : s.status === "skipped" ? "" : "ok" }, s.name + (note ? " — " + note : ""));
  }));
  async function put(body, what) {
    const out = await api.put(reg, body, { suppressToast: true });
    if (what !== false) toast(what || "Saved", "ok");
    S = Object.assign(S, out);
    view.refresh();
    return out;
  }
  const busy = (btn, fn) => async () => { btn.disabled = true; try { await fn(); } finally { btn.disabled = false; } };

  // --- header ------------------------------------------------------------------------------------
  function guestText(row) {
    if (!isRoot()) return ["part of ", el("a", { href: "/services/" + enc(S.parent) + "?tab=subs" }, S.parent),
      row.stack ? [" · stack ", el("a", { class: "mono", href: row.stack.href }, row.stack.name)] : ""];
    if (!hasGuest()) return S.node ? "on " + S.node.name : "no guest";
    return S.guest_kind.toUpperCase() + " " + S.guest_id + (S.node ? " on " + S.node.name : "");
  }
  async function check() {
    try { await O.bulk("services", "resync", [slug]); } catch (_e) { return; }
    view.refresh(); loadCtl(); loadLinked();
  }
  async function wake() { try { await O.bulk("services", "wake", [slug]); } catch (_e) { /* toast */ } view.refresh(); }
  async function act(target, action) {
    const info = ctlInfo && ctlInfo[target];
    const what = info ? info.describe : target;
    if (action !== "start") {
      const ok = await confirm({
        title: action[0].toUpperCase() + action.slice(1) + " " + what + "?",
        body: target === "guest" ? (S.parent ? "This is the whole guest of " + S.root + ": everything on it goes with it." : "The whole guest " + (action === "stop" ? "stops" : "restarts") + ", with everything on it.") : "Anyone using it is cut off meanwhile.",
        action: action[0].toUpperCase() + action.slice(1), danger: action === "stop",
      });
      if (!ok) return;
    }
    try {
      const r = await api.post(base + "/actions/" + action, { target }, { suppressToast: true });
      toast(r.message || action + " sent", "ok");
    } catch (e) { toast(e.message, "error"); }
    setTimeout(() => { loadCtl(); loadLinked(); }, 1500);
  }
  function drawHeader() {
    const row = V.row || { state: { cls: "off", label: "unknown" } };
    $("#svc-name").textContent = S.name || slug;
    const st = $("#svc-state"); st.className = "pill " + row.state.cls; st.textContent = row.state.label; st.title = row.state.why || "";
    const host = (S.proxy_hosts || [])[0];
    fill($("#svc-sub"), guestText(row), S.group ? " · " + S.group : "",
      host ? [" · ", el("a", { href: "https://" + host.hostname, target: "_blank", rel: "noopener" }, host.hostname + " ↗")] : "",
      S.description ? el("span", { class: "faint" }, " · " + S.description) : "");
    $("#wake-banner").hidden = !S.wake_unsupported;
    const asleep = ["asleep", "stopped", "unknown"].includes(row.state.label) && S.enabled;
    const svcCtl = ctlInfo && ctlInfo.service && ctlInfo.service.available;
    const guestCtl = ctlInfo && ctlInfo.guest && ctlInfo.guest.available;
    const canBackup = isRoot() && S.backup_policy_id != null && !S.backup_excluded;
    const box = clear($("#svc-actions"));
    if (asleep) box.append(el("button", { type: "button", onclick: wake }, "Wake"), el("button", { type: "button", class: "secondary", onclick: check }, "Check now"));
    else box.append(el("button", { type: "button", onclick: check }, "Check now"));
    if (svcCtl) box.append(el("button", { type: "button", class: "secondary", onclick: () => act("service", "restart") }, "Restart"));
    if (canBackup) box.append(el("button", { type: "button", class: "secondary", onclick: backupNow }, "Back up now"));
    if (row.stack) box.append(el("a", { class: "btn secondary", href: row.stack.href }, "Manage stack"));
    box.append(menuButton(() => [
      { label: "Edit settings…", onClick: editGeneral },
      svcCtl ? { heading: "This service: " + ctlInfo.service.describe } : null,
      svcCtl ? { label: "Start", onClick: () => act("service", "start") } : null,
      svcCtl ? { label: "Stop…", onClick: () => act("service", "stop") } : null,
      guestCtl ? { heading: "Its guest: " + ctlInfo.guest.describe } : null,
      guestCtl ? { label: "Start the guest", onClick: () => act("guest", "start") } : null,
      guestCtl ? { label: "Restart the guest…", onClick: () => act("guest", "restart") } : null,
      guestCtl ? { label: "Stop the guest…", onClick: () => act("guest", "stop") } : null,
      "-",
      linked && linked.state !== "none" ? { label: linked.state === "off" ? "Switch on" : "Switch off…", onClick: () => doSwitch(linked.state === "off" ? "on" : "off") } : null,
      S.ip && S.ip.has_guest ? { label: "Find its address now", onClick: discover } : null,
      isRoot() && hasGuest() ? { label: "Propose a restore…", onClick: proposeRestore } : null,
      { label: S.enabled ? "Disable" : "Enable", onClick: () => O.bulk("services", S.enabled ? "disable" : "enable", [slug]).then(() => view.refresh(), () => {}) },
      "-",
      { label: "Delete…", danger: true, onClick: () => O.deleteService({ slug, name: S.name || slug }, { rows: (S.children || []).map((c) => ({ slug: c.slug, parent: slug })) }, () => { location.href = "/archive"; }).catch(() => {}) },
    ], "More actions"));
  }

  // --- overview: health ---------------------------------------------------------------------------
  function configSummary(p) {
    const c = p.config || {}; const t = c.transport || {};
    if (p.kind === "http") return (c.method && c.method !== "GET" ? c.method + " " : "") + (c.url || "?") + (c.expect_status && String(c.expect_status) !== "200" ? " → " + c.expect_status.join(",") : "");
    if (p.kind === "tcp") return (c.host || "?") + ":" + (c.port ?? "?");
    if (p.kind === "systemd") return (c.unit || "?") + " via " + (t.type || "local") + (t.host ? " " + t.host : "");
    if (p.kind === "ping") return "ping " + (c.host || "its address (" + ((S.ip && S.ip.effective_ip) || "none yet") + ")") + (t.type && t.type !== "local" ? " via " + t.type + " " + (t.host || "") : "");
    const text = JSON.stringify(c);
    return text.length > 70 ? text.slice(0, 67) + "…" : text;
  }
  function drawHealth() {
    const H = V.health; const row = V.row || { state: { cls: "off", label: "unknown", why: "" } };
    const box = clear($("#health"));
    $("#health-age").textContent = H.verdict_at ? "checked " + fmt.ago(H.verdict_at) : "not checked yet";
    const why = row.state.why || H.reason || (row.state.cls === "ok" ? "Every required probe passes." : "");
    box.append(el("div", { class: "banner " + (row.state.cls === "fail" ? "fail" : row.state.cls === "warn" ? "" : "info"), style: { display: "flex", gap: "10px", alignItems: "flex-start" } },
      el("span", { class: "pill " + row.state.cls }, row.state.label), el("span", {}, why || "Not checked yet: it is checked on the next scan.")));
    const rows = [];
    if (H.guest) {
      const g = H.guest;
      rows.push({ name: "guest gate", checks: "node " + g.node + " " + g.node_state + ", " + g.guest + " " + g.guest_state, state: g.state === "healthy" ? "healthy" : g.state, label: g.state === "healthy" ? "ok" : g.state, took: null, probe: null });
    }
    for (const p of S.probes) {
      const r = H.probe_results[p.name];
      rows.push({ name: p.name, checks: configSummary(p), state: p.enabled === false ? "off" : r ? r.state : "unknown",
        label: p.enabled === false ? "off" : r ? (r.state === "healthy" ? "ok" : r.state) : "not run", why: r ? r.message : "", took: r ? r.latency_ms : null, probe: p });
    }
    if (!rows.length) box.append(el("p", { class: "muted" }, "No probes: it can never be judged healthy, so it is never backed up. Add one on the Configuration tab."));
    else box.append(el("div", { class: "dt-scroll" }, el("table", { class: "dt" },
      el("thead", {}, el("tr", {}, ["Probe", "Checks", "Result", "Took", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows.map((r) => el("tr", {},
        el("td", { class: "first" }, el("strong", {}, r.name)),
        el("td", { class: "mono", "data-label": "Checks", style: { overflowWrap: "anywhere" } }, r.checks),
        el("td", { "data-label": "Result" }, el("span", { class: "pill " + (VERDICT_CLS[r.state] || "off"), title: r.why || null }, r.label)),
        el("td", { class: "mono", "data-label": "Took" }, r.took == null ? "" : r.took >= 1000 ? (r.took / 1000).toFixed(1) + " s" : Math.round(r.took) + " ms"),
        el("td", { class: "menu-cell" }, r.probe ? el("button", { type: "button", class: "ghost small", onclick: () => probeDrawer(r.probe) }, "Edit") : "")))))));
    const strip = el("div", { class: "ticks", "aria-label": "Last 20 checks" });
    box.append(el("div", { class: "lane", style: { margin: "14px 0 6px" } }, "Last 20 checks"), strip);
    api.get(reg + "/health-history?limit=20", { suppressToast: true }).then((list) => {
      if (!list.length) { strip.append(el("span", { class: "faint" }, "no scans yet")); return; }
      for (const h of list) strip.append(el("span", { class: VERDICT_CLS[h.state] || "off", title: fmt.time(h.sampled_at) + " — " + h.state + (h.reason ? ": " + h.reason : "") }));
    }, () => strip.append(el("span", { class: "faint" }, "could not load")));
  }

  // --- overview: facts, sub-services, recent -------------------------------------------------
  function drawFacts() {
    const ip = S.ip || {};
    const policy = V.policy;
    const row = V.row || {};
    const gate = V.health.guest;
    fill($("#facts"), facts([
      ["Runs on", isRoot()
        ? [S.node ? el("a", { href: "/nodes?q=" + enc(S.node.name) }, S.node.name) : "no node", hasGuest() ? [" › ", S.guest_kind.toUpperCase() + " " + S.guest_id, gate ? [" ", el("span", { class: "pill " + (gate.guest_state === "running" ? "ok" : "off") }, gate.guest_state)] : ""] : ""]
        : [el("a", { href: "/services/" + enc(S.parent) }, S.parent), " (sub-service)"]],
      ["Address", ip.effective_ip ? [el("span", { class: "mono" }, ip.effective_ip), el("span", { class: "faint" }, " · " + (ip.effective_source_label || "") + (ip.ip_discovered_at && ip.effective_source !== "manual" ? " " + fmt.ago(ip.ip_discovered_at) : ""))] : el("span", { class: "faint" }, ip.has_guest ? "not found yet" : "none")],
      ["Proxy hosts", (S.proxy_hosts || []).length ? (S.proxy_hosts || []).map((h) => el("div", {}, el("a", { href: "/proxy-hosts?q=" + enc(h.hostname), class: "mono" }, h.hostname), " ", syncPill(h))) : el("span", { class: "faint" }, "none")],
      ["Backup", S.backup_excluded ? "never (" + (S.backup_excluded_reason || "excluded") + ")" : !isRoot() ? "with " + S.parent : policy ? [policy.name, row.backup && row.backup.sub ? " · " + row.backup.sub + (row.backup.at ? " " + fmt.ago(row.backup.at) : "") : ""] : el("span", { class: "pill warn" }, "no policy")],
      ["Wake on hit", wakeText()],
      isRoot() && hasGuest() ? ["Starts with its node", bootFact()] : null,
      ["Waking takes", wakeTime()],
      (S.depends_on || []).length ? ["Depends on", S.depends_on.map((d, i) => [i ? ", " : "", el("a", { href: "/services/" + enc(d) }, d)])] : null,
      S.group ? ["Group", S.group] : null,
    ]), el("button", { type: "button", class: "ghost small", style: { marginTop: "12px", color: "var(--link)", padding: 0 }, onclick: () => T.show("configuration") }, "Edit in Configuration →"));
  }
  function wakeText() {
    const w = S.wake || {};
    if (!S.wake_on_hit_enabled) return "no";
    const yes = S.wake_trigger === "button" ? "yes, with a Start button" : "yes";
    return w.traffic_blocker ? ["not now", el("div", { class: "faint", style: { fontSize: "12.5px" } }, w.traffic_blocker)] : yes;
  }
  function bootFact() {
    const b = (S.wake || {}).boot;
    if (!b) return el("span", { class: "faint" }, "not read yet: read while its node is on (Nodes & guests)");
    if (!b.onboot) return ["no", el("span", { class: "faint" }, " · a wake starts it")];
    return el("span", { title: "Proxmox starts it when the node boots, so a wake that powers the node on leaves it to Proxmox." + (b.order != null ? " Order " + b.order + "." : "") },
      "yes, " + fmt.nth(b.position || 0), b.starts_after_s != null ? el("span", { class: "faint" }, " · ~" + fmt.secs(b.starts_after_s) + " after boot") : null);
  }
  function wakeTime() {
    const w = S.wake || {};
    const e = w.estimate;
    if (!e) return el("span", { class: "faint" }, "—");
    const parts = (x) => x.steps.map((st) => st.what + " (" + fmt.secs(st.seconds) + ")").join(", then ");
    const off = w.estimate_off;
    return el("span", { title: "Now: " + parts(e) + (off ? ".\nFrom off: " + parts(off) : "") + "." + (e.learned ? "" : "\nA guess until it has been woken a few times.") },
      "about " + fmt.secs(e.total_s), off ? el("span", { class: "faint" }, " · from off ~" + fmt.secs(off.total_s)) : null);
  }
  // --- overview: a one-line summary; the Sub-services tab has the rest ---------------------------
  function drawSubs() {
    const subs = V.subs || [];
    $("#subs-card").hidden = !isRoot() && !subs.length;
    const box = clear($("#subs"));
    if (!subs.length) { box.append(el("p", { class: "muted", style: { margin: 0 } }, "None. A sub-service is a part that runs inside this one (a stack, an add-on) and follows it.")); return; }
    const failing = subs.filter((x) => x.enabled && (x.label === "failed" || x.label === "stopped"));
    box.append(el("p", { style: { margin: 0 } }, subs.length + " sub-service" + (subs.length === 1 ? "" : "s") + (failing.length ? ", " : ""),
      failing.length ? el("span", { style: { color: "var(--warn)" } }, failing.length + " failing (" + failing.map((x) => x.name).join(", ") + ")") : null, " · ",
      el("a", { href: "?tab=subs", onclick: (e) => { e.preventDefault(); T.show("subs"); } }, "Sub-services tab →")));
  }

  // --- the Sub-services tab ------------------------------------------------------------------------
  const stackLive = new Map(); // stack href -> {containers} from Portainer, or {error}
  function loadStackLive(x) {
    if (!x.stack || stackLive.has(x.stack.href)) return;
    stackLive.set(x.stack.href, null);
    const q = x.stack.instance ? "?instance=" + enc(x.stack.instance) : "";
    api.get("/api/portainer/endpoints/" + x.stack.endpoint + "/stacks/" + enc(x.stack.name) + q, { suppressToast: true })
      .then((d) => { stackLive.set(x.stack.href, d); drawSubsTab(); }, (e) => { stackLive.set(x.stack.href, { error: e.message }); drawSubsTab(); });
  }
  function whatOf(x) {
    const c = x.controller || {};
    if (x.kind === "stack") {
      const live = stackLive.get(x.stack.href);
      const cs = live && live.containers;
      const bad = cs ? cs.filter((k) => k.state !== "running") : [];
      return [el("span", {}, "stack ", el("a", { href: x.stack.href, class: "mono" }, x.stack.name), x.containers ? " · " + x.containers + " container" + (x.containers === 1 ? "" : "s") : ""),
        el("span", { class: "sub" }, cs ? [el("span", { class: "dots", "aria-hidden": "true" }, cs.map((k) => el("i", { class: k.state === "running" ? (k.health === "unhealthy" ? "warn" : "ok") : "fail" }))),
          bad.length ? bad.map((k) => k.name + " " + k.state).join(", ") : "all running"] : live && live.error ? "Portainer: " + live.error : "…")];
    }
    if (x.kind === "container") return ["container ", el("span", { class: "mono" }, c.container || "")];
    return x.kind ? x.kind : el("span", { class: "faint" }, "—");
  }
  async function setWaits(x, on) {
    try { await api.put("/api/registry/services/" + enc(x.slug), { backup_gate: on ? "required" : "ignore" }, { suppressToast: true }); toast(on ? "A backup of " + (S.name || slug) + " now waits for " + x.name : x.name + ": a backup no longer waits for it", "ok"); }
    catch (e) { toast(e.message, "error"); }
    view.refresh();
  }
  function drawSubsTab() {
    const subs = V.subs || [];
    $("#tab-subs").hidden = !subs.length && !isRoot();
    $("#n-subs").textContent = subs.length ? String(subs.length) : "";
    $("#subs-intro").textContent = "Parts that run inside " + (S.name || slug) + ". A stack is one sub-service: its containers are on its stack page.";
    const row = V.row || { state: {} };
    const failing = subs.filter((x) => x.enabled && (x.label === "failed" || x.label === "stopped"));
    const ban = $("#subs-banner");
    ban.hidden = row.state.label !== "degraded";
    if (!ban.hidden) {
      const f = failing[0];
      fill(ban, el("span", { class: "pill warn" }, "degraded"), " ",
        el("span", { style: { flex: "1 1 300px" } }, (S.name || slug) + " itself is healthy, but ", el("strong", {}, f ? f.name : "a sub-service"), (f ? (f.label === "stopped" ? " is stopped" : " is failing") : " is failing") + (f && f.check && f.check.text ? ": " + f.check.text : "") + ". That is why it shows degraded on the Dashboard, the Services list and the Map."),
        f && f.stack ? el("a", { class: "btn secondary small", href: f.stack.href }, "Manage stack") : null);
    }
    const box = clear($("#subs-table"));
    if (!subs.length) { box.append(el("div", { class: "dt-empty" }, el("strong", {}, "No sub-services"), el("div", {}, "Adopt a Portainer stack that runs here, or add one by hand."))); return; }
    subs.forEach(loadStackLive);
    box.append(el("div", { class: "dt-scroll" }, el("table", { class: "dt" },
      el("thead", {}, el("tr", {}, ["Sub-service", "What", "State", "Checks", "Backup", "Proxy hosts", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, subs.map((x) => el("tr", {},
        el("td", { class: "first", style: x.depth > (S.parent ? 1 : 0) + 1 ? { paddingLeft: 18 + 14 * (x.depth - 1) + "px" } : null },
          el("a", { href: "/services/" + enc(x.slug), style: { fontWeight: "600" } }, x.name), el("span", { class: "sub mono" }, x.slug)),
        el("td", { "data-label": "What" }, whatOf(x)),
        el("td", { "data-label": "State" }, el("span", { class: "pill " + x.cls, title: x.why || null }, x.label)),
        el("td", { "data-label": "Checks" }, x.checks.total ? (x.checks.run ? x.checks.ok + " of " + x.checks.total + " ok" : x.checks.total + " not run yet") : el("span", { class: "faint" }, "none")),
        el("td", { "data-label": "Backup", class: x.backup === "does not wait" ? "faint" : null }, x.backup),
        el("td", { "data-label": "Proxy hosts", class: "mono", style: { fontSize: "12.5px" } }, x.hosts.length ? x.hosts.join(", ") : el("span", { class: "faint" }, "—")),
        el("td", { class: "menu-cell", style: { whiteSpace: "nowrap" } },
          x.stack ? el("a", { class: "btn ghost small", href: x.stack.href }, "Manage stack →") : null,
          menuButton(() => [
            { label: "Open", href: "/services/" + enc(x.slug) },
            x.stack ? { label: "Manage stack", href: x.stack.href } : null,
            { label: "Check now", onClick: () => O.bulk("services", "resync", [x.slug]).then(() => view.refresh(), () => {}) },
            x.backup === "waits for it" ? { label: "A backup does not wait for it", onClick: () => setWaits(x, false) } : { label: "A backup waits for it", onClick: () => setWaits(x, true) },
            "-",
            { label: "Remove…", danger: true, onClick: () => O.deleteService({ slug: x.slug, name: x.name }, { rows: subs.filter((k) => k.parent === x.slug).map((k) => ({ slug: k.slug, parent: x.slug })) }, () => view.refresh()).catch(() => {}) },
          ].filter(Boolean), "Actions for " + x.name))))))));
  }
  $("#subs-add").addEventListener("click", () => O.serviceDrawer({
    view: Object.assign({}, V, { rows: [{ slug, name: S.name || slug }] }), prefill: { parent: slug },
    onSaved: () => view.refresh(),
  }));
  $("#sub-add").addEventListener("click", () => O.serviceDrawer({
    view: Object.assign({}, V, { rows: [{ slug, name: S.name || slug }] }), prefill: { parent: slug },
    onSaved: () => view.refresh(),
  }));
  const recent = region($("#recent"), {
    load: () => api.get("/api/activity?service=" + enc(slug) + "&limit=5", { suppressToast: true }),
    render: (list, box) => { clear(box); if (!list.length) box.append(el("p", { class: "muted", style: { margin: 0 } }, "Nothing yet.")); else list.forEach((ev) => box.append(O.eventRow(ev))); },
    topics: ["run", "audit", "alert"], filter: relevant, wait: 1500, skeletonRows: 3,
  });

  // --- configuration: read-only cards, Edit opens a drawer -------------------------------------
  const card = (title, pairs, onEdit, extra) => el("section", { class: "card" },
    el("div", { class: "card-head" }, el("h2", {}, title), el("span", { style: { flex: "1" } }), extra || null, onEdit ? el("button", { type: "button", class: "secondary small", onclick: onEdit }, "Edit") : null),
    facts(pairs));
  function jsonText(o) { return o && Object.keys(o).length ? JSON.stringify(o) : ""; }
  function drawConfig() {
    const ip = S.ip || {};
    const policyName = (V.policies.find((p) => p.id === S.backup_policy_id) || {}).name;
    const rec = S.recovery_state;
    fill($("#config"), 
      card("General", [["Name", S.name], ["Slug", el("span", { class: "mono" }, slug)], ["Description", S.description], ["Group", S.group], ["Watched", S.enabled ? "yes: checked, woken and backed up" : "no: disabled"]], editGeneral),
      card("Where it runs", [
        ["Part of", S.parent || "nothing: it has its own guest"],
        isRoot() ? ["Node", S.node ? S.node.name : null] : null,
        isRoot() ? ["Guest", hasGuest() ? S.guest_kind.toUpperCase() + " " + S.guest_id : "none"] : null,
        !isRoot() ? ["Backup waits for it", S.backup_gate === "required" ? "yes: must be healthy first" : "no"] : null,
        ["Address", S.ip_address ? [el("span", { class: "mono" }, S.ip_address), " (set by hand)"] : ip.discovered_ip ? [el("span", { class: "mono" }, ip.discovered_ip), " (found via " + (ip.ip_source_label || "?") + ")"] : "not known"],
        ip.mac_addresses && ip.mac_addresses.length ? ["NIC MACs", el("span", { class: "mono" }, ip.mac_addresses.join(" "))] : null,
        ip.ip_discovery_error ? ["Last search", ip.ip_discovery_error] : null,
      ], editWhere, ip.has_guest ? el("button", { type: "button", class: "ghost small", onclick: discover }, "Find now") : null),
      card("Wake and power", [
        ["Wake on hit", S.wake_on_hit_enabled ? "yes" : "no"],
        ["A visit while it is down", S.wake_trigger === "button" ? "shows a Start button" : "starts it"],
        ["Controller", ctlInfo && ctlInfo.service && ctlInfo.service.available ? ctlInfo.service.describe : jsonText(S.controller) || "none: only its guest can be acted on"],
        ["Recovery", jsonText(S.recovery) || "off"],
        rec ? ["Recovery now", [rec.exhausted ? "gave up after " + rec.attempts + " attempt(s)" : rec.attempts ? "attempt " + rec.attempts + " done (" + rec.last_action + " - " + rec.last_result + ")" : "failing on " + rec.failing_scans + " scan(s)", " ",
          el("button", { type: "button", class: "ghost small", style: { color: "var(--link)", padding: 0 }, onclick: async () => { try { await api.post(base + "/recovery/reset"); toast("Recovery reset", "ok"); view.refresh(); } catch (_e) { /* toast */ } } }, "Reset")]] : null,
      ], editPower),
      card("Backup", [
        ["Policy", !isRoot() ? "with " + S.parent : S.backup_excluded ? "never backed up" : policyName || "none"],
        S.backup_excluded ? ["Why never", S.backup_excluded_reason] : null,
        ["Also wakes first", (S.backup_requires || []).join(", ") || "nothing extra"],
        ["Depends on", (S.depends_on || []).join(", ") || "nothing"],
      ], editBackup));
  }

  function editGeneral() {
    drawer({
      title: "Edit " + (S.name || slug), sub: "General",
      values: { name: S.name, description: S.description, group: S.group, enabled: S.enabled },
      note: "Saved at once; services.yaml is written by itself.",
      fields: [
        { key: "name", label: "Name", required: true },
        { key: "description", label: "Description", type: "textarea", rows: 2 },
        { key: "group", label: "Group", placeholder: "optional", help: "Only for sorting and filtering." },
        { key: "enabled", label: "Watched: checked, woken and backed up", type: "checkbox", help: "Off: nothing acts on it and its proxy hosts are parked." },
      ],
      onSave: (v) => put({ name: v.name.trim(), description: v.description || "", group: (v.group || "").trim(), enabled: !!v.enabled }),
    });
  }
  function editWhere() {
    drawer({
      title: "Where " + (S.name || slug) + " runs",
      values: { parent: S.parent || "", node_id: S.node_id == null ? "" : String(S.node_id), guest_kind: S.guest_kind, guest_id: S.guest_id, ip_address: S.ip_address || "", backup_gate: S.backup_gate || "ignore" },
      note: "Saved at once; services.yaml is written by itself.",
      fields: [
        { key: "parent", label: "Part of", type: "select", options: [{ value: "", label: "Nothing: it has its own guest" }].concat(V.parent_choices.map((x) => ({ value: x, label: x }))), help: "A sub-service lives on its parent's node and guest and goes dark with it." },
        { key: "backup_gate", label: "Its parent's backup waits for it", type: "select", options: [{ value: "ignore", label: "No: never checked before a backup" }, { value: "required", label: "Yes: must be healthy first (started if stopped)" }], show: (v) => !!v.parent },
        { key: "node_id", label: "Node", type: "select", options: V.nodes.map((n) => ({ value: String(n.id), label: n.name })), show: (v) => !v.parent },
        { key: "guest_kind", label: "Guest", type: "select", options: V.guest_kinds.map((k) => ({ value: k, label: { vm: "VM", lxc: "LXC container", none: "None" }[k] || k })), show: (v) => !v.parent },
        { key: "guest_id", label: "VMID", type: "number", mono: true, show: (v) => !v.parent && v.guest_kind !== "none" },
        { key: "ip_address", label: "Address by hand", mono: true, placeholder: (S.ip && S.ip.discovered_ip) || "blank = the one found", help: "Wins over the address found through Proxmox, the guest agent, ARP or DHCP." },
      ],
      onSave: (v) => {
        const body = { ip_address: (v.ip_address || "").trim() };
        if (v.parent !== (S.parent || "")) body.parent = v.parent;
        if (v.parent) body.backup_gate = v.backup_gate;
        else Object.assign(body, { node_id: v.node_id === "" ? null : Number(v.node_id), guest_kind: v.guest_kind, guest_id: v.guest_kind === "none" ? null : v.guest_id });
        return put(body);
      },
    });
  }
  function editPower() {
    drawer({
      title: "Wake and power", sub: S.name || slug,
      values: { wake_on_hit_enabled: S.wake_on_hit_enabled, wake_trigger: S.wake_trigger || "call", controller: Object.keys(S.controller || {}).length ? JSON.stringify(S.controller, null, 2) : "", recovery: Object.keys(S.recovery || {}).length ? JSON.stringify(S.recovery, null, 2) : "" },
      note: "Saved at once; services.yaml is written by itself.",
      fields: [
        { key: "wake_on_hit_enabled", label: "Wake on hit", type: "checkbox", help: "A visit to one of its hosts may wake it (its node has the same switch). The Wake button, the CLI and the API are not affected." },
        { key: "wake_trigger", label: "A visit while it is down", type: "select", options: [{ value: "call", label: "Starts it straight away" }, { value: "button", label: "Shows a Start button; nothing starts until it is pressed" }], help: "The button keeps scrapers and monitors from waking it by accident." },
        { key: "controller", label: "Controller (JSON)", type: "textarea", rows: 4, placeholder: '{"type": "portainer", "endpoint": 2, "container": "nginx"}', help: "How to start, stop and restart this service on its own. Empty: only its guest can be acted on." },
        { key: "recovery", label: "Recovery (JSON)", type: "textarea", rows: 4, placeholder: '{"enabled": true, "steps": ["restart"], "max_attempts": 3}', help: "What to try by itself when it fails: restart, restart_parent, restart_guest, propose_restore. Empty: off. See docs/recovery.md." },
      ],
      onSave: (v) => {
        const parse = (key, text) => {
          if (!String(text || "").trim()) return {};
          try { return JSON.parse(text); } catch (e) { const err = new Error("Not valid JSON: " + e.message); err.fieldErrors = { [key]: "Not valid JSON: " + e.message }; throw err; }
        };
        return put({ wake_on_hit_enabled: !!v.wake_on_hit_enabled, wake_trigger: v.wake_trigger, controller: parse("controller", v.controller), recovery: parse("recovery", v.recovery) }).then(() => loadCtl());
      },
    });
  }
  function editBackup() {
    drawer({
      title: "Backup", sub: S.name || slug,
      values: { backup_policy_id: S.backup_policy_id == null ? "" : String(S.backup_policy_id), backup_excluded: S.backup_excluded, backup_excluded_reason: S.backup_excluded_reason || "", backup_requires: (S.backup_requires || []).slice(), depends_on: (S.depends_on || []).slice() },
      note: "Saved at once; services.yaml is written by itself.",
      fields: [
        { key: "backup_policy_id", label: "Policy", type: "select", options: [{ value: "", label: "None" }].concat(V.policies.map((p) => ({ value: String(p.id), label: p.name }))), show: (v) => isRoot() && !v.backup_excluded },
        { key: "backup_excluded", label: "Never back it up", type: "checkbox", help: "A hard exclusion, e.g. the VM that holds the backup datastore." },
        { key: "backup_excluded_reason", label: "Why", required: true, show: (v) => !!v.backup_excluded },
        { key: "backup_requires", label: "Also wake before its backup", type: "checklist", options: V.requirable.map((r) => ({ value: r.slug, label: r.slug, note: r.node ? "(" + r.node + ")" : "(no node)", warn: r.blocker })), help: "Woken first and kept awake until the backup is done, on top of what its policy wakes (its database, say)." },
        { key: "depends_on", label: "Depends on", type: "checklist", options: V.other_slugs.map((x) => ({ value: x, label: x })), help: "Must be healthy before this one is judged; a dependency that is down makes it unknown, never failed. Nothing is woken for it." },
      ],
      onSave: (v) => {
        const body = { backup_excluded: !!v.backup_excluded, backup_excluded_reason: v.backup_excluded ? v.backup_excluded_reason.trim() : "", backup_requires: v.backup_requires, depends_on: v.depends_on };
        if (isRoot()) body.backup_policy_id = v.backup_excluded || v.backup_policy_id === "" ? null : Number(v.backup_policy_id);
        return put(body).then(() => loadPlan());
      },
    });
  }
  async function discover() {
    toast("Looking for its address…");
    try {
      const r = await api.post(base + "/ip/discover", undefined, { suppressToast: true });
      toast(r.status === "found" ? "Found " + r.message + (r.changed ? " (changed)" : "") : r.message || "No address found", r.status === "found" ? "ok" : "error");
    } catch (e) { toast(e.message, "error"); }
    view.refresh();
  }

  // --- configuration: probes -----------------------------------------------------------------------
  const FORM_KINDS = ["http", "tcp", "systemd", "ping"];
  const kindInfo = (k) => V.probe_kinds.find((x) => x.kind === k) || { summary: "", default_timeout_s: 15 };
  function probeValues(p) {
    const c = (p && p.config) || {}; const t = c.transport || {};
    return {
      name: p ? p.name : "", kind: p ? p.kind : "http", required: p ? p.required : true, enabled: p ? p.enabled !== false : true, backup_waits: !!(p && p.backup_waits),
      timeout_s: p ? p.timeout_s : kindInfo("http").default_timeout_s,
      order: p ? p.order : S.probes.reduce((m, x) => Math.max(m, x.order), -1) + 1,
      url: c.url || "", method: c.method || "GET", expect_status: (c.expect_status || [200]).join(", "), expect_body_contains: c.expect_body_contains || "",
      verify_tls: c.verify_tls ?? true, host_header: c.host_header || "", sni_hostname: c.sni_hostname || "",
      tcp_host: c.host || "", tcp_port: c.port ?? null,
      unit: c.unit || "", sd_transport: t.type || "host_agent", sd_host: t.host || "", sd_user: t.user || "", sd_port: t.port ?? null, sd_jump: t.jump_host || "",
      ping_host: p && p.kind === "ping" ? c.host || "" : "", ping_count: c.count ?? 1, ping_transport: p && p.kind === "ping" ? t.type || "local" : "local", ping_thost: p && p.kind === "ping" ? t.host || "" : "",
      config_json: p && !FORM_KINDS.includes(p.kind) ? JSON.stringify(c, null, 2) : "{}",
    };
  }
  function fieldError(key, msg) { const e = new Error(msg); e.fieldErrors = { [key]: msg }; return e; }
  function probeConfig(v, editing) {
    const kind = v.kind;
    const baseCfg = editing && editing.kind === kind ? Object.assign({}, editing.config) : {};
    const set = (o, k, x) => { if (x === "" || x == null) delete o[k]; else o[k] = x; };
    if (!FORM_KINDS.includes(kind)) {
      let parsed;
      try { parsed = JSON.parse(v.config_json || "{}"); } catch (e) { throw fieldError("config_json", "Not valid JSON: " + e.message); }
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw fieldError("config_json", "Must be a JSON object");
      return parsed;
    }
    const c = baseCfg;
    if (kind === "http") {
      set(c, "url", v.url.trim()); c.method = v.method;
      const statuses = String(v.expect_status || "").split(",").map((x) => x.trim()).filter(Boolean);
      if (statuses.some((x) => !/^(\d{3}|[1-5]xx)$/i.test(x))) throw fieldError("expect_status", "Write status codes or classes separated by commas, like 2xx, 3xx, 401");
      c.expect_status = statuses.length ? statuses.map((x) => (/^\d{3}$/.test(x) ? Number(x) : x.toLowerCase())) : [200];
      set(c, "expect_body_contains", v.expect_body_contains); c.verify_tls = !!v.verify_tls;
      set(c, "host_header", v.host_header.trim()); set(c, "sni_hostname", v.sni_hostname.trim());
    } else if (kind === "tcp") {
      set(c, "host", v.tcp_host.trim()); set(c, "port", v.tcp_port == null ? null : Number(v.tcp_port));
    } else if (kind === "systemd") {
      set(c, "unit", v.unit.trim());
      let t = Object.assign({}, c.transport || {}, { type: v.sd_transport });
      if (v.sd_transport === "local") t = { type: "local" };
      else { set(t, "host", v.sd_host.trim()); set(t, "user", v.sd_user.trim()); set(t, "port", v.sd_port == null ? null : Number(v.sd_port)); set(t, "jump_host", v.sd_jump.trim()); }
      c.transport = t;
    } else if (kind === "ping") {
      c.host = (v.ping_host || "").trim(); c.count = v.ping_count == null ? 1 : Number(v.ping_count);
      if (v.ping_transport === "local") delete c.transport;
      else { const t = Object.assign({}, c.transport || {}, { type: v.ping_transport }); set(t, "host", v.ping_thost.trim()); c.transport = t; }
    }
    return c;
  }
  function probeDrawer(p) {
    const editing = p || null;
    const is = (k) => (v) => v.kind === k;
    const d = drawer({
      title: editing ? "Edit probe “" + editing.name + "”" : "Add a probe", sub: (S.name || slug) + " · probe",
      values: probeValues(editing),
      note: "Applies as soon as you save; services.yaml is written for you.",
      saveLabel: editing ? "Save" : "Add probe",
      fields: [
        { key: "name", label: "Name", required: true, mono: true },
        { key: "kind", label: "Kind", type: "select", options: V.probe_kinds.map((k) => ({ value: k.kind, label: k.kind + (k.summary ? " — " + k.summary : "") })) },
        { key: "url", label: "URL", required: true, mono: true, placeholder: "https://app.example.lan/health", show: is("http") },
        { key: "method", label: "Method", type: "select", options: ["GET", "HEAD"], show: is("http") },
        { key: "expect_status", label: "Expected status", mono: true, placeholder: "2xx, 3xx, 401", show: is("http") },
        { key: "expect_body_contains", label: "Body contains", placeholder: "optional", show: is("http") },
        { key: "verify_tls", label: "Verify the certificate", type: "checkbox", show: is("http") },
        { key: "host_header", label: "Host header", mono: true, placeholder: "optional: app.example.lan", help: "When the URL is a proxy's address, like curl --resolve.", show: is("http") },
        { key: "sni_hostname", label: "SNI hostname", mono: true, placeholder: "defaults to the Host header", show: is("http") },
        { key: "tcp_host", label: "Host", required: true, mono: true, show: is("tcp") },
        { key: "tcp_port", label: "Port", type: "number", required: true, show: is("tcp") },
        { key: "unit", label: "Unit", required: true, mono: true, placeholder: "nginx.service", show: is("systemd") },
        { key: "sd_transport", label: "Asked through", type: "select", options: [{ value: "host_agent", label: "the host runner (usually right)" }, { value: "ssh", label: "SSH from the orchestrator" }, { value: "local", label: "inside the orchestrator's container" }], show: is("systemd") },
        { key: "sd_host", label: "Target host", mono: true, placeholder: "inventory host or address", show: (v) => v.kind === "systemd" && v.sd_transport !== "local" },
        { key: "sd_user", label: "SSH user", mono: true, placeholder: "root", show: (v) => v.kind === "systemd" && v.sd_transport === "ssh" },
        { key: "sd_port", label: "SSH port", type: "number", placeholder: "22", show: (v) => v.kind === "systemd" && v.sd_transport === "ssh" },
        { key: "sd_jump", label: "Jump host", mono: true, placeholder: "blank = SSH_JUMP_HOST", show: (v) => v.kind === "systemd" && v.sd_transport === "ssh" },
        { key: "ping_host", label: "Host", mono: true, placeholder: "blank = this service's address", show: is("ping") },
        { key: "ping_count", label: "Count", type: "number", min: 1, max: 10, show: is("ping") },
        { key: "ping_transport", label: "Ping from", type: "select", options: [{ value: "local", label: "the orchestrator's container" }, { value: "host_agent", label: "the host runner" }, { value: "ssh", label: "SSH" }], show: is("ping") },
        { key: "ping_thost", label: "From host", mono: true, show: (v) => v.kind === "ping" && v.ping_transport !== "local" },
        { key: "config_json", label: "Settings (JSON)", type: "textarea", rows: 6, help: "Command-running kinds need a transport, e.g. {\"transport\": {\"type\": \"host_agent\", \"host\": \"media\"}}.", show: (v) => !FORM_KINDS.includes(v.kind) },
        { type: "section", label: "How it counts" },
        { key: "required", label: "Required", type: "checkbox", help: "The service counts as failed when this probe fails, and is not backed up." },
        { key: "enabled", label: "Run it", type: "checkbox", help: "Off: not run and never counted; its settings are kept." },
        { key: "backup_waits", label: "Backup waits for it", type: "checkbox", show: () => !isRoot(), help: "A backup of " + (S.root || "the parent") + " waits for this check to pass." },
        { key: "timeout_s", label: "Timeout (seconds)", type: "number", min: 1 },
        { key: "order", label: "Order", type: "number" },
      ],
      testLabel: "Test the saved probe",
      onTest: editing ? async () => {
        const r = await api.post("/api/registry/probes/" + editing.id + "/test", undefined, { suppressToast: true });
        const text = r.state + (r.latency_ms != null ? " in " + Math.round(r.latency_ms) + " ms" : "") + (r.message ? ": " + r.message : "");
        if (r.state !== "healthy") throw new Error(text);
        return text;
      } : null,
      onDelete: editing ? async () => {
        if (!await confirm({ title: "Delete the probe " + editing.name + "?", action: "Delete" })) return false;
        await api.del("/api/registry/probes/" + editing.id); toast("Deleted " + editing.name, "ok"); view.refresh();
      } : null,
      async onSave(v) {
        const body = { name: v.name.trim(), kind: v.kind, required: !!v.required, enabled: !!v.enabled, backup_waits: !isRoot() && !!v.backup_waits, timeout_s: Number(v.timeout_s) || kindInfo(v.kind).default_timeout_s, order: Number(v.order) || 0, config: probeConfig(v, editing) };
        if (editing) await api.put("/api/registry/probes/" + editing.id, body, { suppressToast: true });
        else await api.post(reg + "/probes", body, { suppressToast: true });
        toast((editing ? "Saved " : "Added ") + body.name, "ok");
        view.refresh();
      },
    });
    const kindSel = d.el.querySelector("#f-kind");
    if (!editing && kindSel) kindSel.addEventListener("change", () => { const t = d.el.querySelector("#f-timeout_s"); t.value = kindInfo(kindSel.value).default_timeout_s; d.set("timeout_s", Number(t.value)); });
  }
  async function addPing() {
    const names = new Set(S.probes.map((p) => p.name));
    let name = "ping";
    for (let i = 2; names.has(name); i++) name = "ping-" + i;
    const config = { host: "", count: 1 };
    if (V.ping_transport) config.transport = V.ping_transport;
    try {
      await api.post(reg + "/probes", { name, kind: "ping", required: false, config }, { suppressToast: true });
      toast("Added the optional probe " + name + ": it pings this service's address", "ok");
      view.refresh();
    } catch (e) { toast(e.message, "error"); }
  }
  async function testProbe(p) {
    try {
      const r = await api.post("/api/registry/probes/" + p.id + "/test", undefined, { suppressToast: true });
      confirm({
        title: p.name + ": " + r.state, danger: false, action: "Close", cancel: "Edit…",
        body: el("div", {}, el("p", {}, (r.latency_ms != null ? Math.round(r.latency_ms) + " ms. " : "") + (r.message || "")),
          r.details && Object.keys(r.details).length ? el("pre", { class: "mono", style: { whiteSpace: "pre-wrap", fontSize: "12px", background: "var(--inset)", padding: "10px", borderRadius: "8px", maxHeight: "40vh", overflow: "auto" } }, JSON.stringify(r.details, null, 2)) : null),
      }).then((closed) => { if (!closed) probeDrawer(p); });
    } catch (e) { toast(e.message, "error"); }
  }
  const probeTable = table($("#probes"), {
    rows: [], key: (p) => p.id, name: (p) => p.name, urlState: false, plain: true, noun: ["probe", "probes"],
    sort: { key: "order", dir: 1 },
    columns: [
      { key: "name", label: "Probe", sort: (p) => p.name, render: (p) => [el("button", { type: "button", class: "ghost", style: { padding: 0, height: "auto", color: "var(--text)", fontWeight: "600" }, onclick: () => probeDrawer(p) }, p.name), p.enabled === false ? [" ", el("span", { class: "pill off", title: "Not run and never counted" }, "off")] : null] },
      { key: "kind", label: "Kind", sort: true },
      { key: "checks", label: "Checks", render: (p) => el("span", { class: "mono", style: { overflowWrap: "anywhere" } }, configSummary(p)) },
      { key: "required", label: "Counts", sort: (p) => (p.required ? 0 : 1), render: (p) => (p.required ? "required" : "optional") },
      { key: "last", label: "Last scan", render: (p) => { const r = V.health.probe_results[p.name]; return r ? el("span", { class: "pill " + (VERDICT_CLS[r.state] || "off"), title: r.message || null }, r.state === "healthy" ? "ok" : r.state) : ""; } },
      { key: "order", label: "#", sort: (p) => p.order, phone: false, render: (p) => el("span", { class: "faint" }, String(p.order)) },
    ],
    menu: (p) => [
      { label: "Edit…", onClick: () => probeDrawer(p) },
      { label: "Test now", onClick: () => testProbe(p) },
      { label: p.enabled === false ? "Run it again" : "Stop running it", onClick: async () => { try { await api.put("/api/registry/probes/" + p.id, { enabled: p.enabled === false }, { suppressToast: true }); view.refresh(); } catch (e) { toast(e.message, "error"); } } },
      "-",
      { label: "Delete…", danger: true, onClick: async () => { if (await confirm({ title: "Delete the probe " + p.name + "?", action: "Delete" })) { try { await api.del("/api/registry/probes/" + p.id); view.refresh(); } catch (_e) { /* toast */ } } } },
    ],
    empty: { title: "No probes", text: "Without one it can never be judged healthy, so it is never backed up.", action: el("button", { type: "button", class: "small", onclick: () => probeDrawer(null) }, "Add a probe") },
  });
  function drawProbes() {
    probeTable.setRows(S.probes);
    $("#n-probes").textContent = S.probes.length || "";
    const tools = clear($("#probe-tools"));
    if (S.ip && S.ip.effective_ip && !S.probes.some((p) => p.kind === "ping")) tools.append(el("button", { type: "button", class: "ghost small", onclick: addPing }, "Add a ping probe"));
    tools.append(el("button", { type: "button", class: "small", onclick: () => probeDrawer(null) }, "+ Add probe"));
  }

  // --- related: on / off --------------------------------------------------------------------------
  const PART_STATE = (p) => p.on === true ? ["ok", p.kind === "proxy host" ? "serving" : p.kind === "node" ? "on" : "running"] : p.on === false ? ["off", "off"] : ["off", "?"];
  function drawLinked(extraSteps) {
    const st = linked;
    const pillEl = $("#onoff-state");
    if (!st) { pillEl.className = "pill off"; pillEl.textContent = "…"; return; }
    const STATE = { on: "ok", off: "off", mixed: "warn", unknown: "off", none: "off" };
    pillEl.className = "pill " + (STATE[st.state] || "off"); pillEl.textContent = st.state === "none" ? "nothing linked" : st.state;
    $("#onoff-note").textContent = st.state === "none" ? "No proxy host, stack, container or guest is linked yet." : "Everything below is switched together, in this order.";
    const box = clear($("#onoff"));
    const note = st.host_down ? "Nothing is running here: " + st.host_down + "." + (st.served ? " It is meant to be on, so wake it." : "") : st.drift ? "Not what was asked for: " + st.drift : "";
    if (note) box.append(el("div", { class: "banner" + (st.drift ? "" : " info") }, note));
    if (st.parts.length) box.append(el("div", { class: "chain" }, st.parts.map((p, i) => {
      const [cls, label] = PART_STATE(p);
      return el("div", { class: "chain-part" },
        el("div", { class: "faint", style: { fontSize: "12px" } }, (i + 1) + " · " + p.kind),
        el("strong", { style: { overflowWrap: "anywhere" } }, p.label),
        el("div", {}, el("span", { class: "pill " + cls }, label), p.deliberate ? el("span", { class: "tag", style: { marginLeft: "6px" } }, "off on its own") : null, p.pending ? el("span", { class: "tag", style: { marginLeft: "6px" } }, "not pushed") : null),
        p.detail ? el("div", { class: "faint", style: { fontSize: "12px", overflowWrap: "anywhere" } }, p.detail) : null,
        p.open_url ? el("a", { href: p.open_url, target: "_blank", rel: "noopener", style: { fontSize: "12px" } }, "Open in Portainer ↗") : null);
    })));
    if (extraSteps) box.append(stepsList(extraSteps));
    const acts = clear($("#onoff-actions"));
    if (st.state === "none") return;
    if (st.state === "off") acts.append(el("button", { type: "button", class: "small", onclick: () => doSwitch("on") }, "Switch on"));
    else acts.append(el("button", { type: "button", class: "secondary small", onclick: () => doSwitch("off") }, "Switch off…"));
    acts.append(menuButton(() => [
      st.state === "mixed" ? { label: "Make everything on", onClick: () => doSwitch("on") } : null,
      st.state === "mixed" ? { label: "Make everything off…", onClick: () => doSwitch("off") } : null,
      { label: "Read again", onClick: loadLinked },
      hasStack() ? { heading: "Stack " + ctl().stack + " only (the proxy is not touched)" } : null,
      hasStack() ? { label: "Start the stack", onClick: () => stackAction("start") } : null,
      hasStack() ? { label: "Restart the stack…", onClick: () => stackAction("restart") } : null,
      hasStack() ? { label: "Stop the stack…", onClick: () => stackAction("stop") } : null,
      { label: "Switches in Activity", href: "/activity?kind=switch&service=" + enc(slug) },
    ], "On / off actions"));
  }
  async function loadLinked() {
    try { linked = await api.get(base + "/linked", { suppressToast: true }); } catch (_e) { linked = linked || null; }
    drawLinked(); drawHeader(); drawCounts();
  }
  async function doSwitch(state) {
    const body = { state };
    const hasGuestPart = linked && linked.parts.some((p) => p.kind === "guest");
    if (state === "off") {
      const guestBox = el("input", { type: "checkbox", id: "sw-guest" });
      const ok = await confirm({
        title: "Switch " + (S.name || slug) + " off?",
        body: el("div", {}, el("p", {}, "Its proxy hosts stop being served, then its stack stops." + ((S.children || []).length ? " Everything below it goes first." : "")),
          isRoot() && hasGuestPart ? el("label", { class: "inline" }, guestBox, " also stop its Proxmox guest") : null),
        affected: linked ? linked.parts.map((p) => p.kind + " " + p.label) : null, action: "Switch off",
      });
      if (!ok) return;
      body.include_guest = guestBox.checked;
    } else if ((S.children || []).length) {
      const kidsBox = el("input", { type: "checkbox" });
      const ok = await confirm({ title: "Switch " + (S.name || slug) + " on?", danger: false, action: "Switch on",
        body: el("div", {}, el("p", {}, "Its stack starts; once it is up its proxy hosts are served again."), el("label", { class: "inline" }, kidsBox, " also start the sub-services that were on (otherwise they are only watched)")) });
      if (!ok) return;
      body.restore_children = kidsBox.checked;
    }
    toast("Switching " + state + "…");
    try {
      const r = await api.post(base + "/switch", body, { suppressToast: true });
      if (r.state) linked = r.state;
      drawLinked(r.steps);
      toast(r.ok ? (S.name || slug) + " is " + (r.state ? r.state.state : state) : "Failed: " + r.message, r.ok ? "ok" : "error");
    } catch (e) { toast(e.message, "error"); }
    view.refresh();
  }
  async function stackAction(action) {
    if (action !== "start" && !await confirm({ title: action[0].toUpperCase() + action.slice(1) + " the stack " + ctl().stack + "?", body: "Only the stack in Portainer; the proxy hosts are not touched (use the switch for that).", action: action[0].toUpperCase() + action.slice(1), danger: action === "stop" })) return;
    try { const r = await api.post("/api/stacks/" + ctl().endpoint + "/" + enc(ctl().stack) + "/" + action, undefined, { suppressToast: true }); toast(r.message || "Done", "ok"); } catch (e) { toast(e.message, "error"); }
    loadLinked();
  }

  // --- related: proxy hosts -----------------------------------------------------------------------
  const SYNC = { pending_create: ["pending", "not pushed"], pending_update: ["pending", "not pushed"], detached: ["warn", "changed in Traefik"], external: ["off", "external"], in_sync: ["ok", "in sync"], local: ["off", "kept here"] };
  function syncPill(h) {
    const live = hostSync[h.id];
    if (live && !live.enabled) return el("span", { class: "pill off" }, "off");
    const s = live && SYNC[live.sync_state];
    return s ? el("span", { class: "pill " + s[0], title: (live.sync_changes || []).join("\n") || null }, s[1]) : el("span", { class: "pill off" }, "…");
  }
  let hostServices = null;
  async function hostDrawer(h) {
    if (!hostServices) { try { hostServices = await api.get("/api/views/proxy-host-services", { suppressToast: true }); } catch (_e) { hostServices = []; } }
    let full = h;
    if (h) { try { full = await api.get("/api/proxy-hosts/" + h.id, { suppressToast: true }); } catch (_e) { /* use what we have */ } }
    O.proxyHostDrawer({ host: full || null, services: hostServices, serviceId: S.id, onSaved: () => { view.refresh(); loadHosts(); } });
  }
  async function hostCall(method, path, body, done) {
    try {
      if (method === "del") await api.del(path, { suppressToast: true });
      else await api.post(path, body, { suppressToast: true });
      toast(done, "ok");
    } catch (e) { toast(e.message, "error"); }
    view.refresh(); loadHosts(); loadLinked();
  }
  function drawHosts() {
    const box = clear($("#hosts"));
    const list = S.proxy_hosts || [];
    if (!list.length) box.append(el("p", { class: "muted", style: { margin: 0 } }, "None. Add one, or link one Traefik already serves."));
    for (const h of list) {
      const live = hostSync[h.id] || {};
      const o = live.route_options || {};
      const opts = [live.cert_resolver ? "HTTPS" : "", o.http_redirect ? "redirect" : "", o.hsts ? "HSTS" : "", h.wake_on_hit ? "wake on hit" : ""].filter(Boolean).join(" + ");
      box.append(el("div", { class: "event-row", style: { gridTemplateColumns: "minmax(0,auto) auto minmax(0,1fr) auto" } },
        el("button", { type: "button", class: "ghost", style: { padding: 0, height: "auto", font: "600 13.5px var(--mono)", color: "var(--link)" }, onclick: () => hostDrawer(h) }, h.hostname),
        syncPill(h),
        el("span", { class: "faint mono", style: { fontSize: "12px", overflowWrap: "anywhere" } }, "→ " + (h.upstream || "nothing yet") + (opts ? " · " + opts : "")),
        menuButton(() => [
          { label: "Edit…", onClick: () => hostDrawer(h) },
          { label: "Open on Proxy hosts", href: "/proxy-hosts?q=" + enc(h.hostname) },
          { label: "Unlink from this service", onClick: () => hostCall("del", base + "/proxy-hosts/" + enc(h.hostname), undefined, "Unlinked " + h.hostname) },
          "-",
          { label: "Delete…", danger: true, onClick: () => O.deleteProxyHosts([{ id: h.id, hostname: h.hostname }], () => { view.refresh(); loadHosts(); }).catch(() => {}) },
        ], "Actions for " + h.hostname)));
    }
    const linkedNames = new Set(list.map((h) => h.hostname));
    const sugg = cands ? cands.candidates.filter((c) => !linkedNames.has(c.hostname)) : [];
    if (sugg.length) box.append(el("div", { style: { marginTop: "12px" } }, el("div", { class: "lane" }, "Point at a port of its stack"),
      el("div", { class: "tags", style: { gap: "6px" } }, sugg.map((c) => el("button", { type: "button", class: "chip", onclick: () => hostCall("post", base + "/proxy-hosts", { hostname: c.hostname, force: !!c.linked_to }, "Linked " + c.hostname) }, (c.linked_to ? "Move " : "Link ") + c.hostname + (c.linked_to ? " (from " + c.linked_to + ")" : ""))))));
    fill($("#hosts-actions"), 
      el("button", { type: "button", class: "ghost small", onclick: () => hostDrawer(null) }, "+ Add"),
      menuButton(() => [{ label: "Link a proxy host…", onClick: linkDrawer }], "More proxy host actions"));
  }
  function linkDrawer() {
    drawer({
      title: "Link a proxy host", sub: "To " + (S.name || slug),
      values: { hostname: "", force: false }, saveLabel: "Link",
      note: "The route changes on the next push to Traefik Manager, which happens by itself.",
      fields: [
        { key: "hostname", label: "Hostname", required: true, mono: true, list: "dl-link-hosts" },
        { key: "force", label: "Move it here if it belongs to another service", type: "checkbox" },
      ],
      async onSave(v) { await api.post(base + "/proxy-hosts", { hostname: v.hostname.trim(), force: !!v.force }, { suppressToast: true }); toast("Linked " + v.hostname, "ok"); view.refresh(); loadHosts(); },
    });
  }
  async function loadHosts() {
    try {
      const all = await api.get("/api/proxy-hosts", { suppressToast: true });
      hostSync = {}; for (const h of all) if (h.service_id === S.id) hostSync[h.id] = h;
    } catch (_e) { /* keep what we had */ }
    try { cands = await api.get(base + "/proxy-host-candidates", { suppressToast: true }); } catch (_e) { cands = null; }
    drawHosts(); drawFacts();
  }

  // --- related: stack and deploys -------------------------------------------------------------------
  let deployRuns = [];
  let deploySteps = null;
  // [healthy before, recent backup, healthy after, keep previous]
  const PRESETS = { none: [false, false, false, false], probes: [true, false, true, false], "probes+backup": [true, true, true, false], "probes+keep3": [true, false, true, true] };
  function guardsText(d) {
    if (!d || !Object.keys(d).length) return "No deploy method yet.";
    const g = d.guard; const bits = [];
    if (g.pre.probes) bits.push("healthy before");
    if (g.pre.backup) bits.push("a backup newer than " + g.pre.backup.max_age_h + " h" + (g.pre.backup.take_if_older ? " (taken if older)" : ""));
    const kp = g.pre.keep_previous;
    if (kp) bits.push("keep the last " + kp.keep + " versions (" + [kp.dump ? "dump" : "", kp.images ? "images" : ""].filter(Boolean).join(" and ") + ")");
    if (g.post.probes) bits.push("healthy within " + g.post.timeout_s + " s after, for " + g.post.settle_s + " s");
    return (d.enabled ? "" : "Deploys are switched off. ") + "Guards: " + (bits.join(", ") || "none") + ". If it fails: " + ({ notify: "notify", stop: "switch it off", restore_backup: "propose a restore" }[d.on_failure] || d.on_failure) + "." + (d.trigger && d.trigger.webhook ? " Webhook: " + d.trigger.webhook.provider + (d.trigger.webhook.branch ? " (" + d.trigger.webhook.branch + ")" : "") + "." : "");
  }
  function drawStack() {
    $("#stack-card").hidden = !hasStack();
    if (!hasStack()) return;
    const part = linked && linked.parts.find((p) => p.kind === "stack");
    $("#stack-where").textContent = "Portainer endpoint " + ctl().endpoint;
    const has = S.deploy && Object.keys(S.deploy).length > 0;
    const box = clear($("#stack"));
    box.append(el("div", { style: { display: "flex", flexWrap: "wrap", gap: "8px", alignItems: "center", marginBottom: "8px" } },
      el("strong", { class: "mono" }, ctl().stack), part ? el("span", { class: "pill " + PART_STATE(part)[0] }, part.detail || PART_STATE(part)[1]) : null),
      el("p", { class: "faint", style: { margin: "0 0 10px", fontSize: "13px" } }, guardsText(S.deploy)),
      el("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap" } },
        has ? el("button", { type: "button", class: "secondary small", onclick: () => deployNow() }, "Deploy now") : null,
        el("button", { type: "button", class: has ? "ghost small" : "small", onclick: deployDrawer }, has ? "Deploy settings" : "Set up deploys")));
    if (deploySteps) box.append(stepsList(deploySteps));
    if (deployRuns.length) {
      const RES = { succeeded: "ok", failed: "fail", skipped: "warn", running: "pending" };
      append(box, el("div", { class: "lane", style: { margin: "14px 0 4px" } }, "Last deploys"),
        deployRuns.slice(0, 6).map((r) => {
          const trig = (r.steps.find((s) => s.name === "trigger") || { detail: {} }).detail || {};
          return el("div", { class: "event-row", style: { gridTemplateColumns: "9rem auto minmax(0,1fr) auto" } },
            el("span", { class: "faint mono", style: { fontSize: "12px" } }, fmt.time(r.started_at)),
            el("span", { class: "pill " + (RES[r.status] || "off") }, r.status),
            el("span", { style: { overflowWrap: "anywhere" } }, (trig.source || "") + (r.error ? " · " + r.error : "")),
            el("button", { type: "button", class: "ghost small", onclick: () => confirm({ title: "Deploy " + fmt.time(r.started_at), body: stepsList(r.steps), action: "Close", danger: false }) }, "Steps"));
        }));
    }
    fill($("#stack-actions"), 
      el("a", { href: "/portainer?q=" + enc(ctl().stack), style: { fontSize: "13px" } }, "Logs"),
      menuButton(() => [
        has ? { label: "Run the guards only", onClick: () => deployNow({ guards: true, guards_only: true }) } : null,
        has ? { label: "Redeploy without guards…", onClick: unguardedDrawer } : null,
        { label: "Deploy settings…", onClick: deployDrawer },
        has && S.deploy.trigger && S.deploy.trigger.webhook ? { label: "Webhook and GitHub…", onClick: webhookDrawer } : null,
        { label: "Deploys in Activity", href: "/activity?kind=deploy&service=" + enc(slug) },
        has ? "-" : null,
        has ? { label: "Remove the deploy method…", danger: true, onClick: async () => { if (await confirm({ title: "Remove the deploy method?", body: "The stack itself is not touched.", action: "Remove" })) { try { await put({ deploy: {} }, "Removed"); } catch (_e) { /* toast */ } } } } : null,
      ], "Stack actions"));
  }
  function deployDrawer() {
    const d = S.deploy && Object.keys(S.deploy).length ? S.deploy : null;
    const g = d ? d.guard : null; const hook = d && d.trigger ? d.trigger.webhook : null;
    const dr = drawer({
      title: "Deploy settings", sub: "How the stack " + ctl().stack + " is redeployed",
      intro: "Portainer holds the stack, its Git repository and its environment; only the guards and the history live here.",
      values: {
        preset: "", pre_probes: g ? !!g.pre.probes : false, backup: g ? !!g.pre.backup : false, backup_age: g && g.pre.backup ? g.pre.backup.max_age_h : 24,
        take: g && g.pre.backup ? !!g.pre.backup.take_if_older : true,
        keep: g ? !!g.pre.keep_previous : false, keep_n: g && g.pre.keep_previous ? g.pre.keep_previous.keep : 3,
        keep_dump: g && g.pre.keep_previous ? !!g.pre.keep_previous.dump : true, keep_images: g && g.pre.keep_previous ? !!g.pre.keep_previous.images : true,
        post_probes: g ? !!g.post.probes : true, timeout_s: g ? g.post.timeout_s : 180, settle_s: g ? g.post.settle_s : 30,
        on_failure: d ? d.on_failure : "notify", pull_image: d ? !!d.pull_image : true, prune: d ? !!d.prune : false, enabled: d ? !!d.enabled : true,
        provider: hook ? hook.provider : "", branch: hook ? hook.branch : "",
      },
      note: "Saved at once; services.yaml is written by itself.",
      fields: [
        { key: "preset", label: "Start from", type: "select", options: [{ value: "", label: "(as it is)" }, { value: "none", label: "None: redeploy and check the stack came up" }, { value: "probes", label: "Probes: healthy before and after" }, { value: "probes+backup", label: "Probes and a recent backup" }, { value: "probes+keep3", label: "Probes, and keep the last 3 versions" }] },
        { type: "section", label: "Before the deploy" },
        { key: "pre_probes", label: "Its probes must be healthy", type: "checkbox" },
        { key: "backup", label: "A verified backup must be recent", type: "checkbox" },
        { key: "backup_age", label: "Newer than (hours)", type: "number", min: 1, show: (v) => v.backup },
        { key: "take", label: "Take one first when it is older", type: "checkbox", show: (v) => v.backup },
        { key: "keep", label: "Keep the version it replaces", type: "checkbox", help: "A database dump and a pre-deploy tag on each image, for rolling back." },
        { key: "keep_n", label: "Versions to keep", type: "number", min: 1, show: (v) => v.keep },
        { key: "keep_dump", label: "Dump the database (needs a dump set up)", type: "checkbox", show: (v) => v.keep },
        { key: "keep_images", label: "Tag the images", type: "checkbox", show: (v) => v.keep },
        { type: "section", label: "After the deploy" },
        { key: "post_probes", label: "Its probes must turn healthy", type: "checkbox" },
        { key: "timeout_s", label: "Within (seconds)", type: "number", min: 1, show: (v) => v.post_probes },
        { key: "settle_s", label: "Then stay healthy for (seconds)", type: "number", min: 0, show: (v) => v.post_probes },
        { key: "on_failure", label: "If it fails", type: "select", options: [{ value: "notify", label: "Notify and leave it as it is" }, { value: "stop", label: "Switch it off and notify" }, { value: "restore_backup", label: "Propose restoring the backup (needs your approval)" }] },
        { type: "section", label: "Redeploy" },
        { key: "pull_image", label: "Pull images", type: "checkbox" },
        { key: "prune", label: "Prune", type: "checkbox" },
        { key: "enabled", label: "Deploys allowed", type: "checkbox" },
        { key: "provider", label: "Webhook", type: "select", options: [{ value: "", label: "None: by hand only" }, { value: "gitlab", label: "GitLab" }, { value: "github", label: "GitHub" }, { value: "generic", label: "Generic (a token)" }] },
        { key: "branch", label: "Branch", mono: true, placeholder: "any", show: (v) => !!v.provider },
      ],
      async onSave(v) {
        const deploy = {
          enabled: !!v.enabled, trigger: { webhook: v.provider ? { provider: v.provider, branch: (v.branch || "").trim() } : null },
          guard: { pre: { probes: !!v.pre_probes, backup: v.backup ? { max_age_h: Number(v.backup_age) || 24, take_if_older: !!v.take } : null,
            keep_previous: v.keep ? { keep: Number(v.keep_n) || 3, dump: !!v.keep_dump, images: !!v.keep_images } : null },
            post: { probes: !!v.post_probes, timeout_s: Number(v.timeout_s) || 180, settle_s: Number(v.settle_s) || 0 } },
          on_failure: v.on_failure, pull_image: !!v.pull_image, prune: !!v.prune,
        };
        await put({ deploy }, "Deploy settings saved");
      },
    });
    const sel = dr.el.querySelector("#f-preset");
    sel.addEventListener("change", () => {
      const p = PRESETS[sel.value]; if (!p) return;
      [["pre_probes", p[0]], ["backup", p[1]], ["post_probes", p[2]], ["keep", p[3]]].forEach(([k, val]) => { const cb = dr.el.querySelector("#f-" + k); if (cb) { cb.checked = val; } dr.set(k, val); });
    });
  }
  function unguardedDrawer() {
    drawer({
      title: "Redeploy without guards", sub: ctl().stack,
      intro: "Skips the checks before the deploy. The checks after it still run. The reason is kept in the audit log.",
      values: { reason: "" }, saveLabel: "Redeploy",
      fields: [{ key: "reason", label: "Why", required: true, type: "textarea", rows: 2 }],
      onSave: (v) => { deployNow({ guards: false, reason: v.reason.trim() }); },
    });
  }
  async function deployNow(body) {
    body = body || { guards: true };
    if (!body.guards_only && body.guards && !await confirm({ title: "Redeploy " + ctl().stack + "?", body: guardsText(S.deploy), action: "Deploy", danger: false })) return;
    let run;
    try { run = await api.post(base + "/deploy", body, { suppressToast: true }); } catch (e) { toast(e.message, "error"); return; }
    toast((body.guards_only ? "Checking the guards" : "Deploying") + " (run #" + run.run_id + ")");
    for (let i = 0; i < 600; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      let cur;
      try { cur = await api.get("/api/deploys/" + run.run_id, { suppressToast: true }); } catch (_e) { continue; }
      deploySteps = cur.steps; drawStack();
      if (cur.status !== "running") {
        const ok = cur.status === "succeeded";
        toast(ok ? (body.guards_only ? "The guards pass" : "Deployed") : (cur.status === "skipped" ? "Blocked: " : "Failed: ") + (cur.error || ""), ok ? "ok" : "error");
        break;
      }
    }
    loadDeploys(); loadLinked();
  }
  async function webhookDrawer() {
    let w;
    try { w = (await api.get(base + "/deploy", { suppressToast: true })).webhook; } catch (e) { toast(e.message, "error"); return; }
    const secretBox = el("div");
    const ghBlock = el("pre", { class: "mono", style: { whiteSpace: "pre-wrap", fontSize: "11.5px", background: "var(--inset)", padding: "10px", borderRadius: "8px", maxHeight: "30vh", overflow: "auto" } }, w.github ? w.github.workflow : "");
    const copy = (text) => (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(() => toast("Copied", "ok"), () => toast("Select it and copy it by hand", "error"));
    const body = el("div", {},
      el("p", {}, w.provider + (w.branch ? ", branch " + w.branch : "") + " → ", el("span", { class: "mono", style: { overflowWrap: "anywhere" } }, w.url)),
      el("p", { class: w.secret_set ? "" : "msg error" }, w.secret_set ? "A secret is set." : "No secret yet: create one before pointing anything at it."),
      el("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap", marginBottom: "12px" } },
        el("button", { type: "button", class: "secondary small", onclick: async () => {
          if (w.secret_set && !await confirm({ title: "Replace the secret?", body: "The old one stops working.", action: "Replace" })) return;
          try {
            const r = await api.post(base + "/deploy/webhook", undefined, { suppressToast: true });
            fill(secretBox, el("div", { class: "banner info" }, el("strong", {}, "Copy the secret now: it cannot be shown again."),
              el("pre", { class: "mono", style: { whiteSpace: "pre-wrap", margin: "8px 0" } }, r.secret),
              el("pre", { class: "mono", style: { whiteSpace: "pre-wrap", fontSize: "11.5px", margin: 0 } }, r.setup.join("\n") + "\n\nGitHub: add a repository secret named " + r.github.secret_name + " with the key above.")));
            ghBlock.textContent = r.github.workflow; w.secret_set = true;
          } catch (e) { toast(e.message, "error"); }
        } }, w.secret_set ? "Replace the secret" : "Create the secret"),
        w.secret_set ? el("button", { type: "button", class: "danger small", onclick: async () => {
          if (!await confirm({ title: "Remove the secret?", body: "The webhook stops working until a new one is created.", action: "Remove" })) return;
          try { await api.del(base + "/deploy/webhook"); toast("Secret removed", "ok"); } catch (_e) { /* toast */ }
        } }, "Remove the secret") : null),
      secretBox,
      w.github ? [el("h3", {}, "GitHub pipeline"),
        el("p", { class: "faint", style: { fontSize: "13px" } }, "Save this as " + w.github.path + " in the repository; its key comes from the repository secret " + w.github.secret_name + "."),
        !w.url_is_public ? el("p", { class: "msg error" }, "No public address is set (ORCHESTRATOR_PUBLIC_URL): replace YOUR-ORCHESTRATOR in the block with an address the runner can reach.") : null,
        ghBlock, el("button", { type: "button", class: "secondary small", style: { marginTop: "8px" }, onclick: () => copy(ghBlock.textContent) }, "Copy the block")] : null);
    drawer({ title: "Webhook", sub: ctl().stack, fields: [{ type: "custom", key: "_w", render: () => body }], saveLabel: "Done", onSave: () => {} });
  }
  async function loadDeploys() {
    if (!hasStack()) return;
    try { deployRuns = await api.get(base + "/deploys?limit=15", { suppressToast: true }); } catch (_e) { /* keep */ }
    drawStack();
  }

  // --- related: backups -------------------------------------------------------------------------------
  let backupSteps = null; let backupRunning = false; let plan = null;
  async function backupNow() {
    if (backupRunning) return;
    backupRunning = true; backupSteps = []; drawBackups(); T.show("related");
    toast("Backing up " + (S.name || slug) + "; waking what it needs first can take minutes");
    let baseId = 0;
    const latest = async () => { const r = await api.get("/api/runs?slug=" + enc(slug) + "&limit=1", { suppressToast: true }); return r.length ? r[0] : null; };
    try { baseId = ((await latest()) || {}).id || 0; } catch (_e) { /* best effort */ }
    let polling = true;
    const poller = (async () => { while (polling) { await new Promise((r) => setTimeout(r, 2000)); if (!polling) break; try { const run = await latest(); if (run && run.id > baseId) { backupSteps = run.steps; drawBackups(); } } catch (_e) { /* keep polling */ } } })();
    try {
      const run = await api.post("/api/backup/" + enc(slug) + "?verify=true", undefined, { suppressToast: true });
      backupSteps = run.steps;
      toast("Backup " + run.status + (run.error ? ": " + run.error : ""), run.status === "succeeded" ? "ok" : "error");
    } catch (e) {
      toast(e.message, "error");
      try { const run = await latest(); if (run && run.id > baseId) backupSteps = run.steps; } catch (_e) { /* nothing more */ }
    } finally { polling = false; await poller; backupRunning = false; backups.refresh(); }
  }
  async function proposeRestore() {
    if (!await confirm({ title: "Propose a restore?", body: "Restores the latest verified backup over " + (S.root || slug) + "'s guest. You approve it on the next page; nothing is restored before that.", action: "Propose" })) return;
    try { const r = await api.post("/api/services/" + enc(S.root || slug) + "/restore-proposal", undefined, { suppressToast: true }); location.href = "/approvals/" + r.token; } catch (e) { toast(e.message, "error"); }
  }
  async function loadPlan() {
    try { plan = await api.get("/api/backup/plan?slug=" + enc(slug), { suppressToast: true }); } catch (_e) { plan = null; }
    drawBackups();
  }
  // What a backup would wake, in what order and how long; what would make it skip.
  function planView(p) {
    const out = el("div", { style: { margin: "0 18px 12px" } });
    for (const sk of p.skips || []) out.append(el("div", { class: "banner warn", style: { marginBottom: "8px" } }, "A backup would be skipped: " + sk.text + "."));
    const tl = p.timeline || { steps: [], nodes: [] };
    const est = Object.fromEntries((tl.steps || []).map((x) => [x.slug, x]));
    if (!p.steps.length && !(p.required_subs || []).length) return out;
    const how = (x) => !x ? "" : x.blocked ? "" : x.steps.map((st) => st.what + " (" + fmt.secs(st.seconds) + ")").join(", then ");
    const det = el("details", { class: "help" },
      el("summary", {}, "What a backup wakes first (" + p.steps.length + ")" + (tl.total_s ? " · about " + fmt.secs(tl.total_s) + (tl.learned ? "" : ", a guess") : "")),
      el("ol", { style: { margin: "6px 0 0", paddingLeft: "20px" } }, p.steps.map((st) => el("li", {}, el("span", { class: "mono" }, st.slug), " on " + (st.node || "?") + " ",
        el("span", { class: "pill " + (st.node_power === "on" || st.node_power === "always_on" ? "ok" : "off") }, st.node_power === "always_on" ? "always on" : st.node_power), " ", el("span", { class: "faint" }, st.reason),
        est[st.slug] && !est[st.slug].blocked ? el("div", { class: "faint", style: { fontSize: "12.5px" } }, "about " + fmt.secs(est[st.slug].total_s) + ": " + how(est[st.slug])) : null,
        st.blocker ? el("div", { class: "field-error" }, st.blocker) : null)),
        (p.required_subs || []).map((x) => el("li", {}, el("span", { class: "mono" }, x.slug), el("span", { class: "faint" }, (x.checks ? " waits for " + x.checks.join(", ") + ";" : "") + (x.can_start ? " sub-service, started through its " + x.controller + " controller if stopped" : " sub-service, must already be healthy"))))),
      (tl.nodes || []).map((n) => el("div", { style: { marginTop: "10px" } },
        el("div", { style: { fontWeight: "600", fontSize: "13px" } }, "When " + n.node + " boots" + (n.delay_s ? " (after " + fmt.secs(n.delay_s) + ")" : "")),
        !n.read_at ? el("div", { class: "faint", style: { fontSize: "12.5px" } }, "Its boot order has not been read yet: it is read while the node is on (Nodes & guests).")
          : !n.sequence.length ? el("div", { class: "faint", style: { fontSize: "12.5px" } }, "Proxmox starts nothing by itself; the backup starts what it needs.")
          : el("ol", { style: { margin: "4px 0 0", paddingLeft: "20px", fontSize: "12.5px" } }, n.sequence.map((g) => el("li", { style: g.needed ? { fontWeight: "600" } : null },
            (g.name || "guest") + " ", el("span", { class: "mono faint" }, String(g.vmid)), el("span", { class: "faint" }, " · ~" + fmt.secs(g.starts_after_s) + " after boot" + (g.needed ? " · this backup needs it" : ""))))))),
      el("p", { class: "faint", style: { fontSize: "12px", margin: "8px 0 0" } }, "Guests set to start at boot are left to Proxmox after a node comes on; the others are started. Times come from the last wakes, or a guess until there are some."));
    out.append(det);
    const pre = p.steps.filter((st) => st.blocker && st.reason !== "backup target");
    if (pre.length) out.append(el("div", { class: "banner fail", style: { marginTop: "8px" } }, "A backup would fail at the wake step unless these are up: " + pre.map((st) => st.slug + ": " + st.blocker).join("; ") + ". Set the node's power target or mark it always on."));
    return out;
  }
  let backupRecords = null;
  function drawBackups() {
    const pol = V.policy;
    const r = (pol && pol.retention) || {};
    const keeps = Object.entries(r).filter(([k, v]) => k.startsWith("keep_") && v).map(([k, v]) => v + " " + k.slice(5)).join(", ");
    $("#backups-policy").textContent = !isRoot() ? "backed up with " + S.parent : S.backup_excluded ? "never backed up: " + (S.backup_excluded_reason || "excluded") : pol ? [pol.name, keeps ? "keeps " + keeps : "", pol.schedule_cron ? "schedule " + pol.schedule_cron : "by hand only"].filter(Boolean).join(" · ") : "no backup policy";
    const acts = clear($("#backups-actions"));
    if (isRoot() && !S.backup_excluded && S.backup_policy_id != null) acts.append(el("button", { type: "button", class: "secondary small", disabled: backupRunning, onclick: backupNow }, backupRunning ? "Backing up…" : "Back up now"));
    if (isRoot() && hasGuest()) acts.append(menuButton(() => [{ label: "Propose a restore…", onClick: proposeRestore }, { label: "Policies", href: "/backup-policies" }], "Backup actions"));
    const box = clear($("#backups"));
    if (backupSteps && backupSteps.length) box.append(el("div", { style: { padding: "0 18px 10px" } }, stepsList(backupSteps)));
    if (plan) box.append(planView(plan));
    if (!isRoot()) { box.append(el("p", { class: "muted", style: { padding: "0 18px 14px", margin: 0 } }, "A sub-service is backed up with its parent's guest: see ", el("a", { href: "/services/" + enc(S.root) + "?tab=related" }, S.root), ".")); return; }
    if (backupRecords === null) { box.append(el("div", { class: "skeleton", style: { padding: "0 18px 14px" } }, el("i"), el("i"))); return; }
    if (!backupRecords.length) { box.append(el("p", { class: "muted", style: { padding: "0 18px 14px", margin: 0 } }, S.backup_excluded ? "Never backed up." : "No backups recorded yet.")); return; }
    box.append(el("div", { class: "dt-scroll" }, el("table", { class: "dt" },
      el("thead", {}, el("tr", {}, ["When", "Kind", "Size", "Checked", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, backupRecords.map((b) => el("tr", {},
        el("td", { class: "first" }, fmt.time(b.created_at), el("span", { class: "sub" }, fmt.ago(b.created_at))),
        el("td", { "data-label": "Kind" }, /^ct\//.test(b.pbs_snapshot_id || "") ? "LXC snapshot" : "VM snapshot"),
        el("td", { class: "mono", "data-label": "Size" }, b.size_bytes == null ? "—" : fmt.bytes(b.size_bytes)),
        el("td", { "data-label": "Checked" }, el("span", { class: "pill " + (b.verified ? "ok" : "off") }, b.verified ? "verified" : "not checked"), b.protected ? el("span", { class: "tag", style: { marginLeft: "6px" } }, "protected") : null),
        el("td", { class: "menu-cell" }, b.pipeline_run_id ? el("a", { href: "/activity?kind=backup&service=" + enc(slug), style: { fontSize: "12px" } }, "run #" + b.pipeline_run_id) : "")))))));
  }
  const backups = region(el("div"), {
    load: () => isRoot() ? api.get("/api/backups?slug=" + enc(slug), { suppressToast: true }) : Promise.resolve([]),
    render: (list) => { backupRecords = list; drawBackups(); },
    topics: ["run"], filter: (d) => relevant(d) && (!d.kind || d.kind === "backup" || d.kind === "restore"), wait: 1500,
  });

  // --- related: dumps -------------------------------------------------------------------------------------
  let dumpInfo = null; let dumpSteps = null; let dumpWatching = false;
  const SQL = ["postgres", "mysql", "mariadb", "mongo"];
  function dumpDrawer() {
    const c = S.dump || {};
    drawer({
      title: "Dump settings", sub: S.name || slug,
      intro: "The database's own dump tool runs inside its container (through Portainer); the output is compressed on the orchestrator host, checked, and the newest are kept. Credentials come from the container's environment.",
      values: { type: c.type || "postgres", database: c.database || "", path: c.path || "", command: c.command || "", container: c.container || "", schedule: c.schedule || "", keep: c.keep || 7, keep_days: c.keep_days || 0, enabled: c.enabled !== false },
      note: "Saved at once; services.yaml is written by itself.",
      fields: [
        { key: "type", label: "What is it", type: "select", options: [{ value: "postgres", label: "PostgreSQL" }, { value: "mysql", label: "MySQL" }, { value: "mariadb", label: "MariaDB" }, { value: "mongo", label: "MongoDB" }, { value: "sqlite", label: "SQLite file" }, { value: "command", label: "Other (a command)" }] },
        { key: "database", label: "Database", mono: true, placeholder: "blank = every database", show: (v) => SQL.includes(v.type) },
        { key: "path", label: "Database file", mono: true, required: true, placeholder: "/data/app.db", show: (v) => v.type === "sqlite" },
        { key: "command", label: "Command", type: "textarea", rows: 2, required: true, placeholder: "runs in the container; its output is the dump", show: (v) => v.type === "command" },
        { key: "container", label: "Container", mono: true, placeholder: "blank = this service's own container" },
        { key: "schedule", label: "Schedule", mono: true, placeholder: "0 3 * * *", help: "Cron, in the time zone of System. Blank: only when you ask." },
        { key: "keep", label: "Keep the newest", type: "number", min: 1 },
        { key: "keep_days", label: "Nothing older than (days)", type: "number", min: 0, help: "0: no limit." },
        { key: "enabled", label: "Dumps allowed", type: "checkbox" },
      ],
      onDelete: S.dump && Object.keys(S.dump).length ? async () => {
        if (!await confirm({ title: "Remove the dump settings?", body: "Files already taken are kept.", action: "Remove" })) return false;
        await put({ dump: {} }, "Removed"); loadDumps();
      } : null,
      deleteLabel: "Remove",
      async onSave(v) {
        const cfg = { type: v.type, schedule: (v.schedule || "").trim(), keep: Number(v.keep) || 7, keep_days: Number(v.keep_days) || 0, enabled: !!v.enabled };
        if (SQL.includes(v.type) && v.database.trim()) cfg.database = v.database.trim();
        if (v.type === "sqlite") cfg.path = v.path.trim();
        if (v.type === "command") cfg.command = v.command.trim();
        if (v.container.trim()) cfg.container = v.container.trim();
        await put({ dump: cfg }, "Dump settings saved"); loadDumps();
      },
    });
  }
  async function followDump(runId) {
    if (dumpWatching) return; dumpWatching = true;
    try {
      for (let i = 0; i < 1800; i++) {
        const r = await api.get("/api/runs/" + runId, { suppressToast: true });
        dumpSteps = r.steps; drawDumps();
        if (r.status !== "running") { toast(r.status === "succeeded" ? "Dump finished" : "Dump failed: " + (r.error || r.status), r.status === "succeeded" ? "ok" : "error"); break; }
        await new Promise((res) => setTimeout(res, 1000));
      }
    } catch (_e) { /* stop following */ } finally { dumpWatching = false; loadDumps(); }
  }
  function drawDumps() {
    const show = hasContainer() || (S.dump && Object.keys(S.dump).length > 0);
    $("#dumps-card").hidden = !show;
    if (!show) return;
    const d = dumpInfo;
    $("#dumps-where").textContent = d && d.configured ? "kept in " + d.directory + (d.config.schedule ? " · " + d.config.schedule : " · only when asked") : "not set up";
    const acts = clear($("#dumps-actions"));
    if (d && d.configured) acts.append(el("button", { type: "button", class: "secondary small", disabled: dumpWatching, onclick: async () => {
      try { const r = await api.post(base + "/dump/run", undefined, { suppressToast: true }); toast("Dumping…"); followDump(r.run_id); } catch (e) { toast(e.message, "error"); }
    } }, dumpWatching ? "Dumping…" : "Dump now"));
    acts.append(el("button", { type: "button", class: d && d.configured ? "ghost small" : "small", onclick: dumpDrawer }, d && d.configured ? "Settings" : "Set up dumps"));
    const box = clear($("#dumps"));
    if (dumpSteps && dumpSteps.length) box.append(el("div", { style: { padding: "0 18px 10px" } }, stepsList(dumpSteps)));
    if (!d || !d.configured) { box.append(el("p", { class: "muted", style: { padding: "0 18px 14px", margin: 0 } }, "Dump this service's database on its own, kept and checked, apart from the guest backups.")); return; }
    const recs = d.records || [];
    if (!recs.length) box.append(el("p", { class: "muted", style: { padding: "0 18px 14px", margin: 0 } }, "No dumps yet."));
    else box.append(el("div", { class: "dt-scroll" }, el("table", { class: "dt" },
      el("thead", {}, el("tr", {}, ["Taken", "Size", "Checked", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, recs.map((r) => el("tr", {},
        el("td", { class: "first" }, fmt.time(r.created_at), el("span", { class: "sub" }, fmt.ago(r.created_at))),
        el("td", { class: "mono", "data-label": "Size" }, fmt.bytes(r.size_bytes)),
        el("td", { "data-label": "Checked" }, el("span", { class: "pill " + (r.verified ? "ok" : "off") }, r.verified ? "complete" : "not checked"), r.protected ? el("span", { class: "tag", style: { marginLeft: "6px" } }, "protected") : null),
        el("td", { class: "menu-cell" }, menuButton(() => [
          { label: "Download", href: "/api/dumps/" + r.id + "/download" },
          { label: r.protected ? "Unprotect" : "Protect from clean-up", onClick: async () => { try { await api.post("/api/dumps/" + r.id + "/protect", { protected: !r.protected }, { suppressToast: true }); loadDumps(); } catch (e) { toast(e.message, "error"); } } },
          "-",
          { label: "Delete…", danger: true, onClick: async () => { if (await confirm({ title: "Delete " + r.file + "?", action: "Delete" })) { try { await api.del("/api/dumps/" + r.id); loadDumps(); } catch (_e) { /* toast */ } } } },
        ], "Actions for this dump"))))))));
    if (d.restore_hint) box.append(el("details", { class: "help", style: { margin: "12px 18px" } }, el("summary", {}, "Putting a dump back (on the Docker host)"),
      el("pre", { class: "mono", style: { whiteSpace: "pre-wrap", fontSize: "12px" } }, d.restore_hint), el("p", {}, "A restore overwrites live data, so it is a command you run on purpose, not a button.")));
  }
  async function loadDumps() {
    if (!hasContainer() && !(S.dump && Object.keys(S.dump).length)) { drawDumps(); return; }
    try { dumpInfo = await api.get(base + "/dump", { suppressToast: true }); } catch (_e) { /* keep */ }
    if (dumpInfo && dumpInfo.last_run && dumpInfo.last_run.status === "running") followDump(dumpInfo.last_run.run_id);
    drawDumps();
  }

  // --- history ---------------------------------------------------------------------------------
  const historyTable = table($("#history"), {
    rows: [], key: (e) => e.type + ":" + e.id, urlState: false, noun: ["event", "events"], placeholder: "Filter the history",
    search: (e) => O.describeEvent(e).text + " " + (e.kind || e.action || ""),
    sort: { key: "at", dir: -1 },
    columns: [
      { key: "at", label: "When", sort: (e) => e.at, render: (e) => [fmt.time(e.at), el("span", { class: "sub" }, fmt.ago(e.at))] },
      { key: "what", label: "What", render: (e) => { const d = O.describeEvent(e); return el("span", { class: "pill " + d.cls }, d.tag); } },
      { key: "text", label: "Detail", render: (e) => { const d = O.describeEvent(e); return [d.text, d.href ? [" ", el("a", { href: d.href }, "open")] : null, e.type === "run" && e.steps && e.steps.length ? [" ", el("button", { type: "button", class: "ghost small", style: { color: "var(--link)", padding: 0, height: "auto" }, onclick: () => confirm({ title: e.kind + " " + fmt.time(e.at), body: stepsList(e.steps), action: "Close", danger: false }) }, "steps")] : null]; } },
    ],
    chips: [
      { key: "problems", label: "Problems", test: (e) => O.describeEvent(e).cls === "fail" },
      { key: "backup", label: "Backups", test: (e) => e.kind === "backup" || e.kind === "restore" },
      { key: "wake", label: "Wakes", test: (e) => e.kind === "wake" },
      { key: "deploy", label: "Deploys", test: (e) => e.kind === "deploy" },
      { key: "change", label: "Changes", test: (e) => e.type === "audit" },
      { key: "alert", label: "Alerts", test: (e) => e.type === "alert" },
    ],
    empty: { title: "Nothing yet" },
  });
  function loadHistory() {
    historyLoaded = true;
    api.get("/api/activity?service=" + enc(slug) + "&limit=200", { suppressToast: true }).then((l) => historyTable.setRows(l), () => {});
  }

  // --- counts, drawing everything, live refresh ------------------------------------------------
  function drawCounts() {
    const n = (S.proxy_hosts || []).length + (hasStack() ? 1 : 0) + (linked && linked.parts.some((p) => p.kind === "guest") ? 1 : 0) + ((S.dump && Object.keys(S.dump).length) ? 1 : 0);
    $("#n-related").textContent = n || "";
  }
  function drawAll() {
    drawHeader(); drawHealth(); drawFacts(); drawSubs(); drawSubsTab(); drawConfig(); drawProbes(); drawHosts(); drawStack(); drawBackups(); drawDumps(); drawCounts();
  }
  async function loadCtl() {
    try { ctlInfo = await api.get(base + "/controller?live=true", { suppressToast: true }); } catch (_e) { ctlInfo = null; }
    drawHeader(); drawConfig();
  }
  const view = region(el("div"), {
    initial: V,
    load: () => api.get("/api/views/service/" + enc(slug), { suppressToast: true }),
    render: (data) => { V = data; S = data.service; drawAll(); if (historyLoaded) loadHistory(); },
    topics: ["health", "registry", "run"], filter: relevant, wait: 1200,
  });
  if (window.OrchLive) {
    window.OrchLive.on(["run", "health", "registry"], (d) => { if (relevant(d)) { loadLinked(); loadHosts(); } }, { wait: 2000 });
    window.OrchLive.on(["run"], (d) => { if (relevant(d) && d.kind === "deploy") loadDeploys(); if (relevant(d) && d.kind === "dump" && !dumpWatching) loadDumps(); }, { wait: 1500 });
    window.OrchLive.on(["registry"], (d) => { if (relevant(d)) loadPlan(); }, { wait: 2000 });
  }
  loadCtl(); loadLinked(); loadHosts(); loadPlan(); loadDeploys(); loadDumps();
  void recent;
})();
