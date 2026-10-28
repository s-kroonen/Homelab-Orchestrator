/* Notifications (templates/notifications.html), to the Notifications wireframe: the rules (a drawer
 * to add and edit, an on/off switch, ⋯ for the rest), recent alerts, the channels and this device
 * (install as an app, alerts on or off, tests, the devices with alerts on).
 * Data: rules and recent alerts embedded at first paint; channels and push devices read after;
 * alerts refresh in place on live "alert" events. */
(function () {
  "use strict";
  const { $, el, clear, fill, api, fmt, region, table, drawer, confirm } = window.UI;
  const V = JSON.parse($("#initial").textContent);
  const toast = (m, k) => window.toast(m, k);
  const enc = encodeURIComponent;
  const CHANNEL_LABEL = { ntfy: "ntfy", email: "E-mail", webhook: "Webhook", push: "Push (devices)" };
  const csv = (s) => (s || "").split(",").map((x) => x.trim()).filter(Boolean);

  // --- rules --------------------------------------------------------------------------------------
  const scopeText = (r) => {
    const parts = [];
    for (const [k, label] of [["slugs", "service"], ["groups", "group"], ["nodes", "node"]]) {
      for (const v of r.scope[k] || []) parts.push([label + " ", el("span", { class: "tag" }, v)]);
    }
    return parts.length ? parts.flatMap((p, i) => (i ? [" · ", p] : [p])) : "everything";
  };
  const whenText = (r) => {
    const bits = [];
    if (r.events.length) bits.push(r.events.join(" · ").replace(/_/g, " "));
    if (r.levels.length) bits.push("on a " + r.levels.join(" or "));
    if (r.min_severity && r.min_severity !== "info") bits.push("at least " + r.min_severity);
    return bits.join(", ") || "everything";
  };
  async function save(rule, body) {
    if (rule) await api.put("/api/notifications/rules/" + rule.id, body, { suppressToast: true });
    else await api.post("/api/notifications/rules", body, { suppressToast: true });
    rules.refresh();
  }
  const bodyOf = (r, over) => Object.assign({ name: r.name, enabled: r.enabled, levels: r.levels, scope: r.scope, events: r.events, min_severity: r.min_severity, channels: r.channels }, over || {});
  function ruleDrawer(rule) {
    const r = rule || { name: "", enabled: true, levels: [], scope: {}, events: [], min_severity: "info", channels: [] };
    drawer({
      title: rule ? "Edit " + rule.name : "Add a rule",
      sub: "Leave a list empty for \"any\". A rule sends what it matches to its channels; without rules everything that matters goes everywhere.",
      values: {
        name: r.name, enabled: r.enabled, events: r.events, levels: r.levels, min_severity: r.min_severity,
        slugs: (r.scope.slugs || []).join(", "), groups: (r.scope.groups || []).join(", "), nodes: (r.scope.nodes || []).join(", "), channels: r.channels,
      },
      saveLabel: rule ? "Save" : "Add rule",
      fields: [
        { key: "name", label: "Name", required: true, placeholder: "Failures to my phone" },
        { key: "enabled", label: "On", type: "checkbox" },
        { type: "section", label: "When" },
        { key: "events", label: "Events", type: "checklist", options: V.events_known.map((e) => ({ value: e, label: e.replace(/_/g, " ") })) },
        { key: "levels", label: "About a", type: "checklist", options: V.levels.map((l) => ({ value: l, label: l === "sub" ? "sub-service" : l })) },
        { key: "min_severity", label: "At least", type: "select", options: V.severities.map((s) => ({ value: s, label: s })) },
        { type: "section", label: "About" },
        { key: "slugs", label: "Only these services", placeholder: "slugs, comma separated", mono: true },
        { key: "groups", label: "Only these groups", placeholder: "groups, comma separated" },
        { key: "nodes", label: "Only these nodes", placeholder: "node names, comma separated", mono: true },
        { type: "section", label: "Sends to" },
        { key: "channels", label: "Channels", type: "checklist", help: "None ticked: every channel.", options: V.channel_names.map((c) => ({ value: c, label: CHANNEL_LABEL[c] || c })) },
      ],
      async onSave(v) {
        await save(rule, { name: v.name, enabled: !!v.enabled, events: v.events || [], levels: v.levels || [], min_severity: v.min_severity,
          scope: { slugs: csv(v.slugs), groups: csv(v.groups), nodes: csv(v.nodes) }, channels: v.channels || [] });
        toast((rule ? "Saved " : "Added ") + v.name, "ok");
      },
    });
  }
  async function toggle(r) {
    try { await save(r, bodyOf(r, { enabled: !r.enabled })); } catch (e) { toast(e.message, "error"); }
  }
  async function remove(r) {
    if (!await confirm({ title: "Delete rule " + r.name + "?", action: "Delete" })) return;
    try { await api.del("/api/notifications/rules/" + r.id); rules.refresh(); } catch (_e) { /* toast */ }
  }
  const rt = table($("#rules"), {
    rows: V.rules, key: (r) => r.id, plain: true, urlState: false, noun: ["rule", "rules"],
    columns: [
      { key: "name", label: "Rule", render: (r) => el("strong", {}, r.name) },
      { key: "when", label: "When", render: (r) => whenText(r) },
      { key: "about", label: "About", render: (r) => scopeText(r) },
      { key: "to", label: "Sends to", render: (r) => r.channels.length ? el("span", { class: "tags" }, r.channels.map((c) => el("span", { class: "tag" }, c))) : el("span", { class: "faint" }, "every channel") },
      { key: "on", label: "On", render: (r) => el("button", { type: "button", class: "switch", role: "switch", "aria-checked": String(r.enabled), "aria-label": (r.enabled ? "Turn off " : "Turn on ") + r.name, onclick: (e) => { e.stopPropagation(); toggle(r); } }) },
      { key: "edit", label: "", render: (r) => el("button", { type: "button", class: "ghost small", onclick: (e) => { e.stopPropagation(); ruleDrawer(r); } }, "Edit") },
    ],
    menu: (r) => [
      { label: "Edit…", onClick: () => ruleDrawer(r) },
      { label: r.enabled ? "Turn off" : "Turn on", onClick: () => toggle(r) },
      { label: "Its alerts", href: "/activity?kind=alert" },
      "-",
      { label: "Delete…", danger: true, onClick: () => remove(r) },
    ],
    empty: { title: "No rules: the defaults apply", text: "Failed, down and recovered (and flapping and exhausted recoveries) go to every channel. Add a rule to narrow it down or route by service, group or node.",
      action: el("button", { type: "button", class: "small", onclick: () => ruleDrawer(null) }, "+ Add rule") },
  });
  const rules = region($("#rules"), {
    initial: V.rules,
    load: () => api.get("/api/notifications/rules", { suppressToast: true }),
    render: (rows) => rt.setRows(rows),
    topics: ["audit"], wait: 1200,
  });
  $("#new-rule").addEventListener("click", () => ruleDrawer(null));

  // --- recent alerts ---------------------------------------------------------------------------------
  const SEV = { info: "ok", warn: "warn", error: "fail", critical: "fail" };
  const EVENT_CLS = { recovered: "ok", recovery_succeeded: "ok", restore_succeeded: "ok", dump_succeeded: "ok", deploy_succeeded: "ok", update_succeeded: "ok", test: "pending" };
  let folded = true;
  function sentText(e) {
    if (e.suppressed) return e.suppressed_reason || "not sent";
    const parts = Object.entries(e.delivered || {});
    if (!parts.length) return "not sent";
    return "sent to " + parts.map(([k, v]) => k + (v === "ok" ? "" : v.startsWith("skipped") ? " (skipped)" : " (failed)")).join(", ");
  }
  function alertRow(e) {
    const cls = EVENT_CLS[e.event] || SEV[e.severity] || "off";
    return el("div", { class: "event-row" + (e.suppressed ? " muted" : "") },
      el("time", { datetime: e.at, class: "mono faint", title: fmt.time(e.at), style: { fontSize: "12px", whiteSpace: "nowrap" } }, fmt.short(e.at)),
      el("span", { class: "pill " + cls }, e.event.replace(/_/g, " ")),
      el("span", { class: "event-text", title: e.body || null }, e.title, el("span", { class: "faint" }, " · " + sentText(e))),
      e.link ? el("a", { href: e.link, class: "faint", style: { fontSize: "12px" } }, "open") : null);
  }
  const recent = region($("#recent"), {
    initial: V.events,
    load: () => api.get("/api/notifications/events?limit=12" + (folded ? "" : "&suppressed=false"), { suppressToast: true }),
    render: (list, box) => {
      clear(box);
      if (!list.length) { box.append(el("p", { class: "muted", style: { padding: "0 18px 8px", margin: 0 } }, "Nothing yet.")); return; }
      box.append(el("div", { style: { padding: "0 18px 6px" } }, list.map(alertRow)));
    },
    topics: ["alert"], wait: 500, age: $("#recent-age"),
  });
  $("#show-folded").addEventListener("click", (e) => {
    folded = !folded; e.currentTarget.setAttribute("aria-pressed", String(folded)); recent.refresh();
  });
  $("#test-btn").addEventListener("click", async () => {
    try {
      const r = await api.post("/api/notifications/test", {}, { suppressToast: true });
      const parts = Object.entries(r.delivered || {});
      const bad = !parts.length || parts.some(([, v]) => v !== "ok");
      toast(parts.length ? parts.map(([k, v]) => k + ": " + v).join(" · ") : r.suppressed_reason || "Nothing was sent: no channel is set up.", bad ? "error" : "ok");
    } catch (e) { toast(e.message, "error"); }
    recent.refresh();
  });

  // --- channels -------------------------------------------------------------------------------------
  api.get("/api/notifications/channels", { suppressToast: true }).then((c) => {
    const on = new Set(c.channels);
    const all = Array.from(new Set(c.known.concat(c.channels)));
    fill($("#channels"), el("div", { class: "notif-channels" }, all.map((name) => el("div", {},
      el("span", { class: "pill " + (on.has(name) ? "ok" : "off") }, on.has(name) ? "on" : "not set up"), " ",
      el("strong", {}, CHANNEL_LABEL[name] || name)))),
    el("p", { class: "faint", style: { fontSize: "12.5px", margin: "8px 0 0" } },
      c.dry_run ? "DRY_RUN is on: every channel stays silent, alerts are only logged here."
        : !c.channels.length ? "No channel is set up: alerts are only logged here." : ""));
  }, (e) => fill($("#channels"), el("p", { class: "faint" }, e.message)));

  // --- this device (PWA + Web Push) ------------------------------------------------------------------
  const P = window.OrchPush;
  async function drawDevice() {
    const s = await P.status();
    fill($("#dev-pill"), el("span", { class: "pill " + (s.subscribed ? "ok" : "off") }, s.subscribed ? "on" : "off"));
    const note = !s.secure ? "Push needs the dashboard's https address; a plain http:// LAN address cannot do it."
      : !s.supported ? "This browser cannot receive push notifications."
      : s.permission === "denied" ? "Notifications are blocked for this site: allow them in the browser's site settings."
      : s.needsHomeScreen ? "On an iPhone or iPad: add this page to the Home Screen first (Share → Add to Home Screen), then open it from there."
      : "Alerts reach this phone or computer even when the dashboard is closed.";
    const box = clear($("#device"));
    box.append(el("p", { class: "faint", style: { fontSize: "13px", margin: "0 0 10px" } }, note));
    const btns = el("div", { class: "notif-dev-actions" });
    if (s.canInstall) btns.append(el("button", { type: "button", class: "secondary", onclick: async () => { try { await P.install(); } catch (e) { toast(e.message, "error"); } drawDevice(); } }, "Install as an app"));
    if (s.subscribed) {
      btns.append(el("button", { type: "button", class: "secondary", onclick: testPush }, "Send a test here"),
        el("button", { type: "button", class: "ghost", onclick: async () => { try { await P.disable(); toast("Turned off on this device", "ok"); } catch (e) { toast(e.message, "error"); } refreshDevice(); } }, "Turn off"));
    } else {
      btns.append(el("button", { type: "button", style: { width: "100%" }, disabled: !(s.supported && s.secure) || s.permission === "denied", onclick: enable }, "Turn on for this device"));
    }
    btns.append(el("button", { type: "button", class: "ghost small", onclick: async () => { try { await P.testLocal(); toast("Shown. If nothing appeared, check the OS notification settings for this browser.", "ok"); } catch (e) { toast(e.message, "error"); } } }, "Show one from this browser"));
    box.append(btns);
    return s;
  }
  async function enable() {
    const label = await new Promise((resolve) => drawer({
      title: "Turn on alerts for this device", values: { label: P.guessLabel() }, saveLabel: "Turn on",
      note: "Your browser asks for permission next.",
      fields: [{ key: "label", label: "Name this device", required: true, help: "So you can tell your devices apart." }],
      async onSave(v) { resolve(v.label.trim()); },
      onClose: () => resolve(null),
    }));
    if (label == null) return;
    try { await P.enable(label); toast("Turned on. Send a test to check it.", "ok"); } catch (e) { toast(e.message, "error"); }
    refreshDevice();
  }
  async function testPush() {
    try {
      const me = (await P.status()).endpoint;
      const r = await api.post("/api/push/test", { endpoint: me }, { suppressToast: true });
      toast(Object.entries(r.results).map(([k, v]) => k + ": " + v).join("; "), r.ok ? "ok" : "error");
    } catch (e) { toast(e.message, "error"); }
    drawDevices();
  }
  async function drawDevices() {
    let list;
    try { list = await api.get("/api/push/subscriptions", { suppressToast: true }); } catch (_e) { return; }
    const mine = (await P.status()).endpoint;
    const box = clear($("#devices"));
    if (!list.length) return;
    box.append(el("div", { class: "label" }, "Devices with alerts on (" + list.length + ")"),
      list.map((d) => el("div", { class: "notif-dev" },
        el("span", { style: { minWidth: 0 } }, el("strong", {}, d.label || d.user_agent || "device " + d.id), d.endpoint === mine ? [" ", el("span", { class: "pill ok" }, "this device")] : null,
          el("span", { class: "faint" }, " · " + d.username + " · since " + fmt.short(d.created_at) + " · " + (d.last_error ? "last failed: " + d.last_error : d.last_ok_at ? "last ok " + fmt.ago(d.last_ok_at) : "not used yet"))),
        el("button", { type: "button", class: "ghost small", onclick: async () => { try { await api.del("/api/push/subscriptions/" + enc(d.id)); } catch (_e) { /* toast */ } refreshDevice(); } }, "Remove"))));
  }
  function refreshDevice() { drawDevice(); drawDevices(); }
  document.addEventListener("orch:installable", drawDevice);
  document.addEventListener("orch:installed", drawDevice);
  P.ready.then(refreshDevice, refreshDevice);
  void recent;
})();
