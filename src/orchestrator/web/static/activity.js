/* Activity (templates/activity.html), to the Activity wireframe: one feed of runs, changes and
 * alerts, newest first, grouped by day; chips, filters and search narrow it on the server
 * (GET /api/activity) and live in the URL, so a link can open it filtered. Click a run for its
 * steps in a drawer, with Retry where that means something.
 * Data: embedded at first paint; refreshed in place on run, audit and alert events. */
(function () {
  "use strict";
  const { $, el, clear, fill, api, fmt, region, drawer, confirm } = window.UI;
  const O = window.Objects;
  const V = JSON.parse($("#initial").textContent);
  const F = Object.assign({}, V.filters);
  const enc = encodeURIComponent;
  const toast = (m, k) => window.toast(m, k);
  const MAX = 200;
  let limit = 100;
  const MAIN = [["backup", "Backups"], ["wake", "Wakes"], ["deploy", "Deploys"], ["admin", "Changes"], ["alert", "Alerts"]];
  const MORE = [["restore", "restores"], ["switch", "on / off"], ["dump", "dumps"], ["format", "route formats"], ["cleanup", "datastore cleanups"]];
  const RUN_CLS = { succeeded: "ok", failed: "fail", running: "pending", pending: "pending", cancelled: "off", partial: "warn", skipped: "warn" };
  const SEV_CLS = { info: "ok", warn: "warn", error: "fail", critical: "fail" };
  const ALERT_OK = new Set(["recovered", "recovery_succeeded", "restore_succeeded", "dump_succeeded", "deploy_succeeded", "update_succeeded"]);
  const OF = { backup: "Backup of", wake: "Woke", restore: "Restore of", switch: "Switched", deploy: "Deploy of", dump: "Dump of", format: "Route format of", cleanup: "Cleanup" };

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
      const head = ev.kind === "cleanup" ? cleanupHead(ev) : null;
      const lead = ev.service ? [what + " ", svcLink(ev.service), verb]
        : head ? ["Cleanup " + head.policy + " of " + head.datastore + verb] : [what.replace(/ of$/, "") + verb];
      return { cls: RUN_CLS[ev.status] || "off", tag: ev.kind, strong: ev.status === "failed" || ev.status === "skipped",
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

  // --- a cleanup run (CleanupRun wireframe, shown in this drawer) -------------------------------------
  function cleanupHead(ev) {
    const first = (ev.steps || []).find((s) => s.name === "cleanup");
    return first ? first.detail : null;
  }
  const CLEAN_STEPS = {
    storage: (d) => d.error ? "could not be woken: " + d.error : d.held && d.held.length ? "woken for the queue · held on " + d.held.join(", ") + " until the last job is done" : d.reason || "",
    space_before: (d) => d.used_pct + "% full · " + fmt.bytes(d.available_bytes) + " free of " + fmt.bytes(d.total_bytes),
    space_after: (d) => d.used_pct + "% full · " + fmt.bytes(d.available_bytes) + " free" + (d.gc_pending_bytes != null ? " · " + fmt.bytes(d.gc_pending_bytes) + " waits for the next GC" : ""),
    prune: (d) => d.reason ? d.reason : [d.removed + " snapshot" + (d.removed === 1 ? "" : "s") + " removed",
      Object.keys(d.by_service || {}).length ? Object.entries(d.by_service).map(([k, v]) => k + " " + v).join(", ") : "",
      (d.unowned || []).length ? (d.unowned.length + " group" + (d.unowned.length === 1 ? "" : "s") + " no policy keeps " + d.unowned_action + ": " + d.unowned.join(", ")) : "",
      d.errors ? "errors: " + Object.entries(d.errors).map(([k, v]) => k + ": " + v).join("; ") : ""].filter(Boolean).join(" · "),
    gc: (d) => d.reason ? d.reason : d.error ? d.error : d.log ? d.log : d.removed ? "removed " + d.removed + (d.pending ? " · " + d.pending + " pending" : "") : d.phase ? "phase " + d.phase + " of 2: " + (d.phase === 1 ? "marking the chunks in use" + (d.of ? " · read " + d.indexes + " of " + d.of + " index files" : "") : "removing the rest") : "started",
    sync_records: (d) => d.reason ? d.reason : d.dropped != null ? d.dropped + " record" + (d.dropped === 1 ? "" : "s") + " of snapshots PBS no longer has dropped" : d.would_drop != null ? "would drop " + d.would_drop + " (dry run)" : d.error || "",
  };
  const CLEAN_LABEL = { storage: "Storage up and held", space_before: "Space before", prune: "Prune by policy", gc: "Garbage collection", space_after: "Space after", sync_records: "Sync backup records", aborted: "Stopped" };
  function cleanupSteps(ev) {
    const steps = (ev.steps || []).filter((s) => s.name !== "cleanup");
    return el("div", { class: "card flush" }, steps.map((s) => {
      const text = (CLEAN_STEPS[s.name] || ((d) => d.error || ""))(s.detail || {});
      const pct = s.name === "gc" && s.status === "running" && s.detail ? s.detail.percent : null;
      return el("div", { class: "event-row", style: { gridTemplateColumns: "6.5rem minmax(0,1fr)" } },
        el("span", {}, el("span", { class: "pill " + (STEP_CLS[s.status] || "off") }, s.status)),
        el("div", {}, el("strong", {}, CLEAN_LABEL[s.name] || s.name.replace(/_/g, " ")), el("div", { class: "faint", style: { fontSize: "12.5px", overflowWrap: "anywhere" } }, text),
          pct != null ? [el("div", { class: "run-progress", role: "img", "aria-label": pct + "%" }, el("span", { style: { width: pct + "%" } })), el("div", { class: "faint", style: { fontSize: "12px" } }, pct + "%" + (s.detail.phase === 1 ? " · then phase 2 removes chunks unused for 24 h" : ""))] : null));
    }));
  }
  async function stopCleanup(ev) {
    let q = null;
    try { q = await api.get("/api/backup-queue", { suppressToast: true }); } catch (_e) { /* below */ }
    const head = cleanupHead(ev) || {};
    const job = q && q.jobs.find((j) => j.kind === "cleanup" && j.state === "running");
    if (!job) { toast("No running cleanup in the queue", "error"); return; }
    if (!await confirm({ title: "Stop the GC of " + (head.policy || job.name) + "?", body: "PBS stops its garbage collection; it starts over next time. The run ends as failed and the queue moves on.", action: "Stop the GC" })) return;
    try { await api.del("/api/backup-queue/" + job.id, { suppressToast: true }); toast("Stopping the GC", "ok"); } catch (e) { toast(e.message, "error"); }
  }

  // --- a run, in the drawer -----------------------------------------------------------------------------
  const STEP_CLS = { ok: "ok", failed: "fail", indeterminate: "warn", skipped: "off", running: "pending" };
  function retryOf(ev) {
    if (!ev.service || (ev.status !== "failed" && ev.status !== "skipped")) return null;
    const action = { backup: "backup", wake: "wake" }[ev.kind];
    if (!action) return null;
    return async () => { try { await O.bulk("services", action, [ev.service]); toast("Started again", "ok"); } catch (_e) { /* toast */ } feed.refresh(); };
  }
  function runDrawer(ev) {
    const d = describe(ev);
    const retry = retryOf(ev);
    const steps = ev.steps || [];
    drawer({
      title: ev.kind === "cleanup" && cleanupHead(ev) ? "Cleaning " + cleanupHead(ev).datastore + " · " + cleanupHead(ev).policy : (OF[ev.kind] ? OF[ev.kind] + " " + (ev.service || "") : ev.kind).trim(),
      sub: ["run " + ev.id, fmt.short(ev.started_at) + (ev.finished_at ? " → " + fmt.short(ev.finished_at).split(" ").pop() : " · still running"), ev.actor ? "by " + ev.actor : ""].filter(Boolean).join(" · "),
      saveLabel: retry ? "Retry" : "Done",
      async onSave() { if (retry) await retry(); },
      fields: [{ type: "custom", render: () => el("div", {},
        el("p", {}, el("span", { class: "pill " + d.cls }, ev.status)),
        ev.error ? el("div", { class: "banner fail" }, ev.error) : null,
        el("div", { class: "sys-actions" }, ev.service ? el("a", { class: "btn secondary small", href: "/services/" + enc(ev.service) }, "Open service") : null,
          ev.kind === "backup" ? el("a", { class: "btn secondary small", href: "/backup-policies?tab=snapshots" }, "Snapshots") : null),
        ev.kind === "cleanup" ? [
          el("p", { class: "faint" }, (cleanupHead(ev) || {}).reason ? "Why: " + cleanupHead(ev).reason : ""),
          ev.status === "running" ? el("div", { class: "sys-actions" }, el("button", { type: "button", class: "secondary small", onclick: () => stopCleanup(ev) }, "Stop the GC…"),
            el("a", { class: "btn ghost small", href: "/backup-policies?tab=cleanup" }, "The queue")) : el("div", { class: "sys-actions" }, el("a", { class: "btn secondary small", href: "/backup-policies?tab=cleanup" }, "Cleanup policies")),
          el("h3", { class: "ai-h" }, "Steps"), cleanupSteps(ev),
          ev.status === "running" ? el("p", { class: "faint", style: { fontSize: "12.5px" } }, "The run waits for PBS to finish. The queue holds the storage: the next job starts when this is done, and anything else that needs it joins the queue instead of waking it again. If the orchestrator restarts, PBS finishes the GC on its own and the hold expires.") : null] : null,
        ev.kind === "cleanup" ? null : el("h3", { class: "ai-h" }, "Steps"),
        ev.kind === "cleanup" ? null : steps.length ? el("ol", { class: "run-steps" }, steps.map((s) => el("li", {}, el("span", { class: "pill " + (STEP_CLS[s.status] || "off") }, s.status), " " + String(s.name).replace(/_/g, " "),
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
