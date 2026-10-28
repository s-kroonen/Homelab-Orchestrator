/* Activity (templates/activity.html), to the Activity wireframe: one feed of runs, changes and
 * alerts, newest first, grouped by day; chips, filters and search narrow it on the server
 * (GET /api/activity) and live in the URL, so a link can open it filtered. Click a run for its
 * steps in a drawer, with Retry where that means something.
 * Data: embedded at first paint; refreshed in place on run, audit and alert events. */
(function () {
  "use strict";
  const { $, el, clear, fill, api, fmt, region, drawer } = window.UI;
  const O = window.Objects;
  const V = JSON.parse($("#initial").textContent);
  const F = Object.assign({}, V.filters);
  const enc = encodeURIComponent;
  const toast = (m, k) => window.toast(m, k);
  const MAX = 200;
  let limit = 100;
  const MAIN = [["backup", "Backups"], ["wake", "Wakes"], ["deploy", "Deploys"], ["admin", "Changes"], ["alert", "Alerts"]];
  const MORE = [["restore", "restores"], ["switch", "on / off"], ["dump", "dumps"], ["format", "route formats"]];
  const RUN_CLS = { succeeded: "ok", failed: "fail", running: "pending", pending: "pending", cancelled: "off", partial: "warn" };
  const SEV_CLS = { info: "ok", warn: "warn", error: "fail", critical: "fail" };
  const ALERT_OK = new Set(["recovered", "recovery_succeeded", "restore_succeeded", "dump_succeeded", "deploy_succeeded", "update_succeeded"]);
  const OF = { backup: "Backup of", wake: "Woke", restore: "Restore of", switch: "Switched", deploy: "Deploy of", dump: "Dump of", format: "Route format of" };

  // --- the filters ---------------------------------------------------------------------------------
  const kinds = $("#f-kinds");
  function drawKinds() {
    fill(kinds, el("button", { type: "button", class: "chip", "aria-pressed": String(!F.kind), onclick: () => set("kind", "") }, "All"),
      MAIN.map(([k, label]) => el("button", { type: "button", class: "chip", "aria-pressed": String(F.kind === k), onclick: () => set("kind", F.kind === k ? "" : k) }, label)),
      el("select", { "aria-label": "Other kinds", class: MORE.some(([k]) => k === F.kind) ? "on" : null, onchange: (e) => set("kind", e.target.value) },
        el("option", { value: MORE.some(([k]) => k === F.kind) ? "" : F.kind }, "More…"), MORE.map(([k, label]) => el("option", { value: k, selected: F.kind === k }, label))));
  }
  const sel = (key, label, options) => el("select", { "aria-label": label, onchange: (e) => set(key, e.target.value) },
    el("option", { value: "" }, label + ": any"), options.map(([v, l]) => el("option", { value: v, selected: String(F[key] || "") === String(v) }, l)));
  const count = el("span", { class: "count" });
  function drawSelects() {
    const o = V.options;
    fill($("#f-selects"),
      sel("service", "Service", o.slugs.map((s) => [s, s])),
      sel("node", "Node", o.nodes.map((n) => [n, n])),
      o.guests.length ? sel("guest", "Guest", o.guests.map((g) => [g.key, g.label])) : null,
      o.groups.length ? sel("group", "Group", o.groups.map((g) => [g, g])) : null,
      el("select", { "aria-label": "When", onchange: (e) => set("since_hours", e.target.value ? Number(e.target.value) : null) },
        [["", "When: all time"], [1, "last hour"], [24, "last 24 h"], [168, "last 7 days"], [720, "last 30 days"]].map(([v, l]) => el("option", { value: String(v), selected: String(F.since_hours || "") === String(v) }, l))),
      count);
  }
  function set(key, value) {
    F[key] = value; limit = 100;
    writeUrl(); drawKinds(); drawSelects(); feed.refresh();
  }
  function query() {
    const q = new URLSearchParams();
    for (const k of ["service", "group", "node", "guest", "kind", "q"]) if (F[k]) q.set(k, F[k]);
    if (F.problems) q.set("problems", "true");
    if (F.since_hours) q.set("since_hours", String(F.since_hours));
    return q;
  }
  function writeUrl() { const q = query(); history.replaceState(null, "", "/activity" + (q.toString() ? "?" + q : "")); }
  const qIn = $("#f-q");
  qIn.value = F.q || "";
  let qt = 0;
  qIn.addEventListener("input", () => { clearTimeout(qt); qt = setTimeout(() => set("q", qIn.value.trim()), 300); });
  const prob = $("#f-problems");
  prob.setAttribute("aria-pressed", String(!!F.problems));
  prob.addEventListener("click", () => { prob.setAttribute("aria-pressed", String(!F.problems)); set("problems", !F.problems); });

  // --- one event, in words --------------------------------------------------------------------------
  const svcLink = (slug) => (slug ? el("a", { href: "/services/" + enc(slug), onclick: (e) => e.stopPropagation() }, slug) : null);
  function describe(ev) {
    if (ev.type === "run") {
      const what = ev.kind === "wake" && ev.status === "running" ? "Waking" : OF[ev.kind] || ev.kind;
      const verb = ev.status === "succeeded" || what === "Waking" ? "" : ev.status === "failed" ? " failed" : " " + ev.status;
      const lead = ev.service ? [what + " ", svcLink(ev.service), verb] : [what.replace(/ of$/, "") + verb];
      return { cls: RUN_CLS[ev.status] || "off", tag: ev.kind, strong: ev.status === "failed",
        text: [lead, el("span", { class: "faint" }, [ev.error ? " · " + ev.error : "", ev.actor ? " · by " + ev.actor : ""].join(""))] };
    }
    if (ev.type === "alert") {
      const sent = ev.suppressed ? "not sent: " + (ev.suppressed_reason || "muted") : Object.keys(ev.delivered || {}).length ? "sent to " + Object.keys(ev.delivered).join(", ") : "not sent";
      return { cls: ev.suppressed ? "off" : ALERT_OK.has(ev.event) ? "ok" : SEV_CLS[ev.severity] || "off", tag: "alert", href: ev.link,
        text: [ev.title, el("span", { class: "faint" }, " · " + sent)] };
    }
    const action = String(ev.action || "").replace(/[._]/g, " ");
    const target = String(ev.target || "");
    const slug = target.startsWith("service:") ? target.slice(8) : null;
    return { cls: ev.result === "ok" ? "pending" : "fail", tag: "change",
      text: [action + " ", slug ? svcLink(slug) : el("span", { class: "mono" }, target.replace(/^[a-z_]+:/, "")), el("span", { class: "faint" }, " · by " + (ev.actor || "someone") + (ev.result !== "ok" ? " · " + ev.result : ""))] };
  }
  function dayOf(iso) {
    const d = new Date(iso); const today = new Date(); const y = new Date(); y.setDate(today.getDate() - 1);
    const same = (a, b) => a.toDateString() === b.toDateString();
    return same(d, today) ? "Today" : same(d, y) ? "Yesterday" : fmt.short(iso).replace(/ \d\d:\d\d$/, "");
  }
  function row(ev) {
    const d = describe(ev);
    const click = ev.type === "run" ? () => runDrawer(ev) : null;
    return el("div", { class: "event-row" + (click ? " clickable" : ""), onclick: click, tabindex: click ? "0" : null,
      onkeydown: click ? (e) => { if (e.key === "Enter") click(); } : null },
      el("time", { datetime: ev.at, class: "mono faint", title: fmt.time(ev.at), style: { fontSize: "12px" } }, fmt.short(ev.at).split(" ").pop()),
      el("span", {}, el("span", { class: "pill " + d.cls }, d.tag)),
      el("span", { class: "event-text", style: d.strong ? { fontWeight: "600" } : null }, d.text),
      click ? el("button", { type: "button", class: "ghost small", onclick: (e) => { e.stopPropagation(); click(); } }, "open")
        : d.href ? el("a", { href: d.href, class: "faint", style: { fontSize: "12px" } }, "open") : el("span"));
  }

  // --- a run, in the drawer -----------------------------------------------------------------------------
  const STEP_CLS = { ok: "ok", failed: "fail", indeterminate: "warn", skipped: "off", running: "pending" };
  function retryOf(ev) {
    if (!ev.service || ev.status !== "failed") return null;
    const action = { backup: "backup", wake: "wake" }[ev.kind];
    if (!action) return null;
    return async () => { try { await O.bulk("services", action, [ev.service]); toast("Started again", "ok"); } catch (_e) { /* toast */ } feed.refresh(); };
  }
  function runDrawer(ev) {
    const d = describe(ev);
    const retry = retryOf(ev);
    const steps = ev.steps || [];
    drawer({
      title: (OF[ev.kind] ? OF[ev.kind] + " " + (ev.service || "") : ev.kind).trim(),
      sub: ["run " + ev.id, fmt.short(ev.started_at) + (ev.finished_at ? " → " + fmt.short(ev.finished_at).split(" ").pop() : " · still running"), ev.actor ? "by " + ev.actor : ""].filter(Boolean).join(" · "),
      saveLabel: retry ? "Retry" : "Done",
      async onSave() { if (retry) await retry(); },
      fields: [{ type: "custom", render: () => el("div", {},
        el("p", {}, el("span", { class: "pill " + d.cls }, ev.status)),
        ev.error ? el("div", { class: "banner fail" }, ev.error) : null,
        el("div", { class: "sys-actions" }, ev.service ? el("a", { class: "btn secondary small", href: "/services/" + enc(ev.service) }, "Open service") : null,
          ev.kind === "backup" ? el("a", { class: "btn secondary small", href: "/backup-policies?tab=snapshots" }, "Snapshots") : null),
        el("h3", { class: "ai-h" }, "Steps"),
        steps.length ? el("ol", { class: "run-steps" }, steps.map((s) => el("li", {}, el("span", { class: "pill " + (STEP_CLS[s.status] || "off") }, s.status), " " + String(s.name).replace(/_/g, " "),
          s.detail && Object.keys(s.detail).length ? el("div", { class: "faint mono", style: { overflowWrap: "anywhere" } }, Object.entries(s.detail).map(([k, v]) => k + "=" + (typeof v === "object" ? JSON.stringify(v) : v)).join(", ")) : null)))
          : el("p", { class: "faint" }, "No steps recorded yet."),
        (ev.audit || []).length ? [el("h3", { class: "ai-h" }, "What was recorded"), el("div", {}, ev.audit.map((a) => el("div", { class: "faint", style: { fontSize: "12.5px" } }, fmt.short(a.at) + " · " + a.action + " (" + a.result + ") · " + a.actor)))] : null,
        el("p", { class: "faint", style: { fontSize: "12px", marginTop: "14px" } }, "correlation ", el("span", { class: "mono" }, ev.correlation_id || "—"))) }],
    });
  }

  // --- the feed ----------------------------------------------------------------------------------------
  const foot = $("#feed-foot");
  const feed = region($("#feed"), {
    initial: V.events,
    load: () => { const q = query(); q.set("limit", String(limit)); return api.get("/api/activity?" + q, { suppressToast: true }); },
    render: (events, box) => {
      clear(box);
      count.textContent = events.length + (events.length === 1 ? " event" : " events");
      if (!events.length) {
        const filtered = query().toString();
        box.append(el("div", { class: "dt-empty" }, el("strong", {}, filtered ? "Nothing matches" : "Nothing has happened yet"),
          filtered ? el("div", { style: { marginTop: "10px" } }, el("a", { class: "btn secondary small", href: "/activity" }, "Clear the filters")) : null));
      }
      let day = null;
      for (const ev of events) {
        const d = dayOf(ev.at);
        if (d !== day) { box.append(el("div", { class: "act-day" }, d)); day = d; }
        box.append(row(ev));
      }
      fill(foot, events.length >= limit && limit < MAX
        ? el("button", { type: "button", class: "secondary small", onclick: () => { limit = MAX; feed.refresh(); } }, "Show older")
        : el("span", { class: "faint" }, events.length >= MAX ? "The newest " + MAX + " are shown; narrow the filters for older ones." : ""));
    },
    topics: ["run", "audit", "alert"], wait: 1200, age: $("#feed-age"),
  });
  drawKinds();
  drawSelects();
})();
