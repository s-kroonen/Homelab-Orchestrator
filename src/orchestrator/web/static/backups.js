/* Backups (templates/backups.html), to the Backups wireframe: three tiles, then the policies,
 * the PBS snapshots, the dumps and the orchestrator's own backups as tabs.
 * Data: GET /api/views/backups (the database); the datastore, the snapshots and the
 * orchestrator's backups are read when their part is shown. */
(function () {
  "use strict";
  const { $, el, clear, fill, api, fmt, region, table, drawer, confirm, menuButton, tabs } = window.UI;
  const O = window.Objects;
  const toast = (m, k) => window.toast(m, k);
  const enc = encodeURIComponent;
  let V = JSON.parse($("#initial").textContent);
  let datastore = null;

  // --- small helpers -----------------------------------------------------------------------
  const DAYS = ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays"];
  function cronText(cron) {
    if (!cron) return "by hand only";
    const p = cron.trim().split(/\s+/);
    if (p.length !== 5 || !/^\d+$/.test(p[0]) || !/^\d+$/.test(p[1])) return cron;
    const at = String(p[1]).padStart(2, "0") + ":" + String(p[0]).padStart(2, "0");
    if (p[2] === "*" && p[3] === "*" && p[4] === "*") return "every day " + at;
    if (p[2] === "*" && p[3] === "*" && /^[0-6]$/.test(p[4])) return DAYS[Number(p[4])] + " " + at;
    if (/^\d+$/.test(p[2]) && p[3] === "*" && p[4] === "*") return "day " + p[2] + " of the month " + at;
    return cron;
  }
  function keepsText(r) {
    r = r || {};
    const bits = [];
    if (r.keep_last) bits.push(r.keep_last + " last");
    if (r.keep_daily) bits.push(r.keep_daily + " daily");
    if (r.keep_monthly) bits.push(r.keep_monthly + " monthly");
    return (bits.join(" · ") || "everything") + (r.protect_latest_verified ? " · newest verified kept" : "");
  }
  function whenText(iso) {
    if (!iso) return "—";
    const d = new Date(iso); const now = new Date();
    const time = fmt.time(iso).split(" ").pop().slice(0, 5);
    if (d.toDateString() === now.toDateString()) return time;
    const tomorrow = new Date(now.getTime() + 86400000);
    if (d.toDateString() === tomorrow.toDateString()) return "tomorrow " + time;
    return fmt.time(iso).slice(0, 11) + " " + time;
  }
  const runsOf = (p) => V.last_runs.filter((r) => p.services.includes(r.service));

  // --- tiles --------------------------------------------------------------------------------
  function drawTiles() {
    const runs = V.last_runs;
    const ok = runs.filter((r) => r.status === "succeeded");
    const failed = runs.filter((r) => r.status === "failed");
    const skipped = runs.filter((r) => r.status === "skipped");
    const next = V.policies.filter((p) => p.next_run).sort((a, b) => a.next_run.localeCompare(b.next_run))[0];
    const tile = (label, value, sub, extra) => el("div", { class: "stat-tile" }, el("div", { class: "stat-label" }, label), el("div", { class: "stat-value" }, value), extra || null, sub ? el("div", { class: "stat-sub" }, sub) : null);
    const ds = datastore;
    const frac = ds && ds.reachable && ds.total_bytes ? ds.used_fraction : null;
    fill($("#tiles"),
      tile("Last 24 hours", runs.length ? ok.length + " of " + runs.length + " ok" : "no backups",
        failed.length ? [el("span", { style: { color: "var(--warn)" } }, failed.map((r) => r.service).slice(0, 3).join(", ") + " failed"), " · ", el("a", { href: "#last-card" }, "see why")]
          : skipped.length ? [el("span", { style: { color: "var(--warn)" } }, skipped.length + " skipped"), " · ", el("a", { href: "#last-card" }, "see why")]
          : runs.length ? "all went through" : "nothing ran in the last day"),
      tile("Datastore " + (ds ? ds.name : "") + " on PBS", frac == null ? (ds ? "not reachable" : "…") : Math.round(frac * 100) + "% full",
        frac == null ? (ds && ds.error ? ds.error : "") : fmt.bytes(ds.available_bytes) + " free",
        frac == null ? null : el("div", { class: "bar", role: "img", "aria-label": Math.round(frac * 100) + "% used" }, el("span", { class: frac > 0.95 ? "fail" : frac > 0.85 ? "warn" : "ok", style: { width: Math.round(frac * 100) + "%" } }))),
      tile("Next run", next ? whenText(next.next_run) : V.scheduler_running ? "nothing scheduled" : "scheduler off",
        next ? (next.cleanup_policy && next.cleanup_when === "before" ? next.cleanup_policy + " → " : "") + next.name + " · " + next.services.length + " service" + (next.services.length === 1 ? "" : "s") + (next.cleanup_policy && next.cleanup_when === "after" ? " → " + next.cleanup_policy : "")
          : V.scheduler_running ? "" : "SCHEDULER_ENABLED is false: backups run only by hand"));
  }

  // --- policies -------------------------------------------------------------------------------
  async function runNow(p) {
    if (!await confirm({ title: "Run " + p.name + " now?", body: "Backs up its " + p.services.length + " service(s) one by one, waking what is needed first, like its schedule does.", affected: p.services, affectedLabel: "Services", action: "Run now", danger: false })) return;
    try { await api.post("/api/backup-policies/" + enc(p.name) + "/run", {}, { suppressToast: true }); toast("Started " + p.name + ": follow it in the list below", "ok"); }
    catch (e) { toast(e.message, "error"); }
  }
  async function history(p) {
    let runs = [];
    try { runs = await api.get("/api/runs?policy_id=" + p.id + "&limit=30", { suppressToast: true }); } catch (_e) { /* toast */ }
    const RES = { succeeded: "ok", failed: "fail", running: "pending", skipped: "warn" };
    confirm({
      title: p.name + ": recent runs", action: "Close", danger: false,
      body: runs.length ? el("div", { style: { maxHeight: "55vh", overflowY: "auto" } }, runs.map((r) => el("div", { class: "event-row", style: { gridTemplateColumns: "8.5rem auto minmax(0,1fr)" } },
        el("span", { class: "faint mono", style: { fontSize: "12px" } }, fmt.time(r.started_at)),
        el("span", { class: "pill " + (RES[r.status] || "off") }, r.status),
        el("span", {}, (r.service || r.service_slug || "") + (r.error ? ": " + r.error : ""))))) : el("p", {}, "No runs yet."),
    });
  }
  const policyTable = table($("#policies"), {
    rows: V.policies, key: (p) => p.name, name: (p) => p.name, noun: ["policy", "policies"], urlState: false, plain: true,
    sort: { key: "name", dir: 1 },
    columns: [
      { key: "name", label: "Policy", sort: true, render: (p) => [el("button", { type: "button", class: "ghost", style: { padding: 0, height: "auto", color: "var(--text)", fontWeight: "600" }, onclick: () => policyDrawer(p) }, p.name),
        el("span", { class: "sub" }, (p.wake_before_backup ? "wakes what is off" + (p.restore_power_state ? ", puts it back after" : "") : "does not wake anything") + (p.requires.length ? " · needs " + p.requires.join(", ") : "")),
        p.requires_blockers.length ? el("span", { class: "sub", style: { color: "var(--fail)" } }, p.requires_blockers.map((b) => b.slug).join(", ") + " cannot be woken") : null,
        p.schedule_error ? el("span", { class: "sub", style: { color: "var(--fail)" } }, p.schedule_error) : null] },
      { key: "when", label: "When", render: (p) => [cronText(p.schedule_cron), p.next_run ? el("span", { class: "sub" }, "next " + whenText(p.next_run)) : null] },
      { key: "keeps", label: "Keeps", render: (p) => keepsText(p.retention) },
      { key: "cleanup", label: "Cleanup", render: (p) => p.cleanup_policy ? [p.cleanup_policy, el("span", { class: "sub" }, p.cleanup_when + " the backups")] : el("span", { class: "faint" }, "—") },
      { key: "services", label: "Services", sort: (p) => p.services.length, render: (p) => el("span", { title: p.services.join(", ") || null }, String(p.services.length)) },
      { key: "last", label: "Last run", render: (p) => {
        const runs = runsOf(p);
        if (!runs.length) return el("span", { class: "pill off" }, "—");
        const ok = runs.filter((r) => r.status === "succeeded").length;
        const newest = runs.map((r) => r.started_at).sort().pop();
        return el("span", { class: "pill " + (ok === runs.length ? "ok" : ok ? "warn" : "fail") }, ok + "/" + runs.length + " · " + fmt.ago(newest));
      } },
      { key: "act", label: "", phone: false, cls: "actions", render: (p) => el("button", { type: "button", class: "secondary small", disabled: !p.services.length, onclick: () => runNow(p) }, "Run now") },
    ],
    menu: (p) => [
      { label: "Edit…", onClick: () => policyDrawer(p) },
      { label: "Run now", onClick: () => runNow(p) },
      { label: "Recent runs…", onClick: () => history(p) },
      { label: "Prune now…", onClick: () => pruneDrawer(p) },
      "-",
      { label: "Delete…", danger: true, onClick: () => deletePolicy(p) },
    ],
    empty: { title: "No backup policies yet", text: "A policy says when a set of services is backed up and how long the backups are kept.", action: el("button", { type: "button", class: "small", onclick: () => policyDrawer(null) }, "New policy") },
  });
  async function deletePolicy(p) {
    if (!await confirm({ title: "Delete the policy " + p.name + "?", body: "Its services are no longer backed up on a schedule. Their backups in PBS stay. The policy waits 30 days in the Archive.", affected: p.services, affectedLabel: "Services left without a policy", typeName: p.name, action: "Delete" })) return;
    try { await api.del("/api/backup-policies/" + enc(p.name)); toast("Deleted " + p.name, "ok"); } catch (_e) { return; }
    view.refresh();
  }

  function policyDrawer(p) {
    const editing = !!p;
    const r = (p && p.retention) || {};
    const groupBtns = (v, set) => {
      if (!V.groups.length) return el("span");
      return el("div", { class: "tags", style: { gap: "6px" } }, el("span", { class: "faint", style: { fontSize: "12px" } }, "Select a group:"),
        V.groups.concat([""]).map((g) => el("button", { type: "button", class: "chip", onclick: () => {
          const slugs = V.services.filter((s) => !s.excluded && (s.group || "") === g).map((s) => s.slug);
          const cur = new Set(v.services);
          const all = slugs.every((s) => cur.has(s));
          slugs.forEach((s) => (all ? cur.delete(s) : cur.add(s)));
          const next = Array.from(cur); set("services", next);
          for (const cb of d.el.querySelectorAll("#f-services input")) cb.checked = cur.has(cb.value);
        } }, g || "ungrouped")));
    };
    const d = drawer({
      title: editing ? "Edit " + p.name : "New backup policy",
      sub: editing ? "Backup policy" : "When a set of services is backed up, and how long it is kept",
      values: {
        name: editing ? p.name : "", schedule_cron: editing ? p.schedule_cron : "0 3 * * *", mode: editing ? p.mode : "snapshot",
        keep_last: r.keep_last ?? null, keep_daily: r.keep_daily ?? null, keep_monthly: r.keep_monthly ?? null, protect: !!r.protect_latest_verified,
        services: editing ? p.services.slice() : [], requires: editing ? p.requires.slice() : [],
        wake_before_backup: editing ? p.wake_before_backup : true, wake_mode: editing ? p.wake_mode : "follow",
        max_attempts: editing ? p.max_attempts : 3, restore_power_state: editing ? p.restore_power_state : true,
        cleanup_policy: editing ? p.cleanup_policy || "" : "", cleanup_when: editing ? p.cleanup_when || "before" : "before",
      },
      saveLabel: editing ? "Save" : "Create policy",
      note: "Saved at once; services.yaml is written and the schedule updated by themselves.",
      fields: [
        { key: "name", label: "Name", required: true, mono: true, readonly: editing, help: editing ? "A policy cannot be renamed: delete it and make a new one." : "" },
        { key: "schedule_cron", label: "Schedule (cron)", mono: true, placeholder: "0 3 * * *", help: "In the time zone of System. Blank: only when you press Run now. The time is when the waking starts: leave room for booting." },
        { key: "mode", label: "Snapshot mode", type: "select", options: V.modes.map((m) => ({ value: m, label: { snapshot: "Snapshot: the guest keeps running", suspend: "Suspend the guest meanwhile", stop: "Stop the guest meanwhile" }[m] || m })) },
        { type: "section", label: "What is kept" },
        { key: "keep_last", label: "Keep the last", type: "number", min: 0, placeholder: "not set" },
        { key: "keep_daily", label: "Keep daily", type: "number", min: 0, placeholder: "not set" },
        { key: "keep_monthly", label: "Keep monthly", type: "number", min: 0, placeholder: "not set" },
        { key: "protect", label: "Always keep the newest verified backup", type: "checkbox" },
        { type: "section", label: "Which services" },
        { type: "custom", key: "_groups", render: groupBtns },
        { key: "services", label: "Backed up by this policy", type: "checklist", options: V.services.filter((s) => !s.excluded).map((s) => ({ value: s.slug, label: s.slug, note: s.group ? "(" + s.group + ")" : "" })), help: V.services.some((s) => s.excluded) ? "Services marked never back up are not listed." : "" },
        { type: "section", label: "Waking" },
        { key: "wake_before_backup", label: "Wake the services being backed up first", type: "checkbox", help: "Powers the node on, starts the guest and waits until it is healthy, right before its backup." },
        { key: "wake_mode", label: "Wake switches", type: "select", options: [{ value: "follow", label: "Follow the nodes' wake on hit (a node with it off is not woken)" }, { value: "always", label: "Always wake, also a node with wake on hit off" }, { value: "never", label: "Never wake anything (use what is up)" }] },
        { key: "requires", label: "Also needs, for every backup", type: "checklist", options: V.requirable.map((x) => ({ value: x.slug, label: x.slug, note: x.node ? "(" + x.node + ")" : "(no node)", warn: x.blocker })), help: "Woken once before the batch and kept up until it is done: the storage VM the datastore lives on, say." },
        { key: "max_attempts", label: "Tries", type: "number", min: 1, max: 20, help: "A blocked or unhealthy service is tried again after a pause, up to this many times, then you are told." },
        { key: "restore_power_state", label: "Put it back afterwards", type: "checkbox", help: "Stops what the backup started and powers the node off again, unless something else uses it." },
        { type: "section", label: "Cleanup" },
        { key: "cleanup_policy", label: "Cleanup policy", type: "select", options: [{ value: "", label: "No cleanup" }].concat((V.cleanups || []).map((c) => ({ value: c.name, label: c.name + " · " + (c.datastore || V.datastore_default || "") + " · " + doesText(c) }))),
          help: (V.cleanups || []).length ? "" : "No cleanup policies yet: make one on the Cleanup tab." },
        { key: "cleanup_when", label: "When", type: "select", show: (v) => !!v.cleanup_policy, options: [{ value: "before", label: "Before the backups: frees space first, so they have room" }, { value: "after", label: "After the backups: prunes what tonight's backups made old" }],
          help: "Both run in one queue: the storage is woken once, the node goes back off after the last. With after, a datastore too full to back up is cleaned first anyway. A cleanup failing does not stop the backups." },
      ],
      onDelete: editing ? async () => { await deletePolicy(p); return false; } : null,
      async onSave(v) {
        const retention = {};
        for (const k of ["keep_last", "keep_daily", "keep_monthly"]) if (v[k] != null && v[k] !== "") retention[k] = Number(v[k]);
        if (v.protect) retention.protect_latest_verified = true;
        const body = { schedule_cron: (v.schedule_cron || "").trim(), mode: v.mode, retention, services: v.services, requires: v.requires,
          wake_before_backup: !!v.wake_before_backup, wake_mode: v.wake_mode, max_attempts: Number(v.max_attempts) || 3, restore_power_state: !!v.restore_power_state,
          cleanup_policy: v.cleanup_policy || "", cleanup_when: v.cleanup_when || "before" };
        if (editing) await api.put("/api/backup-policies/" + enc(p.name), body, { suppressToast: true });
        else await api.post("/api/backup-policies", Object.assign({ name: v.name.trim() }, body), { suppressToast: true });
        toast((editing ? "Saved " : "Created ") + (editing ? p.name : v.name.trim()), "ok");
        view.refresh();
      },
    });
  }

  // --- cleanup (BackupsCleanup wireframes; docs/cleanup.md) -----------------------------------------
  const GC_TEXT = (c) => c.gc_mode === "off" ? "no GC" : c.gc_mode === "above" ? "GC over " + c.gc_above_pct + "% full" : c.gc_interval_h ? "GC at most every " + c.gc_interval_h + " h" : "GC every run";
  function doesText(c) { return [c.prune ? "prune by policy" : "no prune", GC_TEXT(c)].join(" · "); }
  function cleanupWhen(c) {
    const parts = c.used_by.map((u) => [u.when + " ", el("a", { href: "?tab=policies&edit=" + enc(u.policy) }, u.policy)]);
    return [parts.length ? parts.map((p, i) => [i ? ", " : "", p]) : null,
      c.schedule_cron ? el("span", { class: parts.length ? "sub" : "" }, (parts.length ? "and " : "") + cronText(c.schedule_cron)) : null,
      !parts.length && !c.schedule_cron ? el("span", { class: "faint" }, "by hand only") : null,
      c.next_run ? el("span", { class: "sub" }, "next " + whenText(c.next_run)) : null,
      c.schedule_error ? el("span", { class: "sub", style: { color: "var(--fail)" } }, c.schedule_error) : null];
  }
  function lastRunCell(c) {
    const r = c.last_run;
    if (!r) return el("span", { class: "pill off" }, "never ran");
    if (r.status === "running") return [el("span", { class: "pill pending" }, "running"), el("span", { class: "sub" }, "since " + whenText(r.started_at))];
    const took = r.finished_at ? Math.max(1, Math.round((new Date(r.finished_at) - new Date(r.started_at)) / 60000)) + " min" : "";
    if (r.status === "failed") return [el("span", { class: "pill fail" }, "failed · " + fmt.ago(r.started_at)), el("span", { class: "sub", style: { overflowWrap: "anywhere" } }, (r.error || "").slice(0, 140))];
    const freed = r.gc && r.gc.removed ? "freed " + r.gc.removed : r.gc && r.gc.status === "skipped" ? "no GC" : "ok";
    return [el("span", { class: "pill ok" }, "ok · " + freed), el("span", { class: "sub" }, [whenText(r.started_at), took ? "took " + took : "", r.removed != null ? r.removed + " pruned" : ""].filter(Boolean).join(" · "))];
  }
  async function runCleanup(c) {
    if (!await confirm({ title: "Run " + c.name + " now?", body: "Queued behind whatever is running: wakes " + (c.requires.join(", ") || "nothing") + ", prunes by the backup policies, runs garbage collection when due and waits for it, then puts the node back. Follow it in Activity.", action: "Queue it", danger: false })) return;
    try { await api.post("/api/cleanup-policies/" + enc(c.name) + "/run", {}, { suppressToast: true }); toast("Queued " + c.name, "ok"); loadQueue(); }
    catch (e) { toast(e.message, "error"); }
  }
  async function deleteCleanup(c) {
    if (!await confirm({ title: "Delete the cleanup policy " + c.name + "?", body: "Nothing in PBS changes. Backup policies that run it stop doing so.", affected: c.used_by.map((u) => u.policy), affectedLabel: "Backup policies left without a cleanup", typeName: c.name, action: "Delete" })) return;
    try { await api.del("/api/cleanup-policies/" + enc(c.name)); toast("Deleted " + c.name, "ok"); } catch (_e) { return; }
    view.refresh();
  }
  const cleanupTable = table($("#cleanups"), {
    rows: V.cleanups || [], key: (c) => c.name, name: (c) => c.name, noun: ["cleanup policy", "cleanup policies"], urlState: false, plain: true,
    sort: { key: "name", dir: 1 },
    columns: [
      { key: "name", label: "Cleanup policy", sort: true, render: (c) => [el("button", { type: "button", class: "ghost", style: { padding: 0, height: "auto", color: "var(--text)", fontWeight: "600" }, onclick: () => cleanupDrawer(c) }, c.name),
        el("span", { class: "sub" }, c.requires.length ? "wakes " + c.requires.join(", ") + (c.restore_power_state ? ", puts it back after" : "") : "wakes nothing"),
        c.requires_blockers.length ? el("span", { class: "sub", style: { color: "var(--fail)" } }, c.requires_blockers.map((b) => b.slug).join(", ") + " cannot be woken") : null] },
      { key: "datastore", label: "Datastore", render: (c) => el("span", { class: "mono" }, c.datastore || (V.datastore_default || "default")) },
      { key: "when", label: "When", render: cleanupWhen },
      { key: "does", label: "Does", render: doesText },
      { key: "last", label: "Last run", render: lastRunCell },
      { key: "act", label: "", phone: false, cls: "actions", render: (c) => el("button", { type: "button", class: "secondary small", onclick: () => runCleanup(c) }, "Run now…") },
    ],
    menu: (c) => [
      { label: "Edit…", onClick: () => cleanupDrawer(c) },
      { label: "Run now…", onClick: () => runCleanup(c) },
      c.last_run ? { label: "Last run in Activity", href: "/activity?kind=cleanup" } : null,
      "-",
      { label: "Delete…", danger: true, onClick: () => deleteCleanup(c) },
    ].filter(Boolean),
    empty: { title: "No cleanup policies yet", text: "A cleanup policy prunes old snapshots and runs PBS's garbage collection while the orchestrator has the storage awake.", action: el("button", { type: "button", class: "small", onclick: () => cleanupDrawer(null) }, "New cleanup policy") },
  });

  function cleanupDrawer(c) {
    const editing = !!c;
    drawer({
      title: editing ? "Edit cleanup policy" : "New cleanup policy",
      sub: editing ? c.name : "Prune and garbage-collect a PBS datastore while it is awake",
      values: {
        name: editing ? c.name : "", datastore: editing ? c.datastore : "",
        schedule_cron: editing ? c.schedule_cron : "", prune: editing ? c.prune : true,
        prune_unowned: editing ? c.prune_unowned_keep_last != null : false, prune_unowned_keep_last: editing && c.prune_unowned_keep_last != null ? c.prune_unowned_keep_last : 3,
        gc_mode: editing ? c.gc_mode : "interval", gc_interval_h: editing ? c.gc_interval_h : 20, gc_above_pct: editing ? c.gc_above_pct : 80,
        max_wait_h: editing ? c.max_wait_h : 6, requires: editing ? c.requires.slice() : [], wake_mode: editing ? c.wake_mode : "follow",
        restore_power_state: editing ? c.restore_power_state : true,
      },
      saveLabel: editing ? "Save" : "Create cleanup policy",
      note: "Saved at once; services.yaml is written and the schedule updated by themselves.",
      fields: [
        { key: "name", label: "Name", required: true, mono: true, readonly: editing },
        { key: "datastore", label: "Datastore", mono: true, placeholder: V.datastore_default || "the configured one", help: "Blank: the datastore the backups go to." },
        { type: "section", label: "When" },
        { type: "custom", key: "_used", label: "With backup policies that use it", render: () => editing && c.used_by.length
          ? el("div", { class: "help" }, c.used_by.map((u, i) => [i ? "; " : "", el("a", { href: "?tab=policies&edit=" + enc(u.policy) }, u.policy), " runs it " + u.when + " its backups"]), ". They share one wake.")
          : el("div", { class: "help" }, "None yet. Set it on a backup policy (its Cleanup section), before or after its backups.") },
        { key: "schedule_cron", label: "Also on its own schedule (cron)", mono: true, placeholder: "blank: only with the backup policies", help: "Wakes what it needs, like a backup policy. When something else holds the storage then, it joins that queue instead of waking it again." },
        { type: "section", label: "Prune" },
        { key: "prune", label: "Prune each service's snapshots by its backup policy", type: "checkbox", help: "The keep rules stay on the backup policy. Pinned and newest verified snapshots are never removed." },
        { key: "prune_unowned", label: "Also prune groups no policy keeps", type: "checkbox", help: "Off: they are listed in the run and left alone." },
        { key: "prune_unowned_keep_last", label: "Keep the last", type: "number", min: 1, show: (v) => !!v.prune_unowned },
        { type: "section", label: "Garbage collection" },
        { key: "gc_mode", label: "Run PBS's garbage collection", type: "select", options: [{ value: "interval", label: "Every run, at most every N hours" }, { value: "above", label: "Only when the datastore is over a fill level" }, { value: "off", label: "Never (prune only)" }], help: "This is what frees the space. PBS only removes chunks nothing has used for 24 h, so what tonight's prune frees comes back at the next GC." },
        { key: "gc_interval_h", label: "At most every (hours)", type: "number", min: 0, show: (v) => v.gc_mode === "interval" },
        { key: "gc_above_pct", label: "Over (% full)", type: "number", min: 1, max: 100, show: (v) => v.gc_mode === "above" },
        { type: "section", label: "Waiting" },
        { key: "max_wait_h", label: "Wait for PBS at most (hours)", type: "number", min: 1, max: 72, help: "The storage stays on and held until GC finishes. Past this, the GC is stopped in PBS (it starts over next time) and the node is put back." },
        { type: "section", label: "Wake and power" },
        { key: "requires", label: "Needs", type: "checklist", options: V.requirable.map((x) => ({ value: x.slug, label: x.slug, note: x.node ? "(" + x.node + ")" : "(no node)", warn: x.blocker })), help: "The storage the datastore lives on. In a queue it is already up and stays held." },
        { key: "wake_mode", label: "Wake switches", type: "select", options: [{ value: "follow", label: "Follow the nodes' wake on hit" }, { value: "always", label: "Always wake" }, { value: "never", label: "Never wake anything (use what is up)" }] },
        { key: "restore_power_state", label: "Put it back afterwards", type: "checkbox", help: "After the last job in the queue, a node that was off is powered off again, unless something else needs it." },
      ],
      onDelete: editing ? async () => { await deleteCleanup(c); return false; } : null,
      async onSave(v) {
        const body = { datastore: (v.datastore || "").trim(), schedule_cron: (v.schedule_cron || "").trim(), prune: !!v.prune,
          prune_unowned_keep_last: v.prune_unowned ? Number(v.prune_unowned_keep_last) || 3 : null, gc_mode: v.gc_mode,
          gc_interval_h: Number(v.gc_interval_h) || 0, gc_above_pct: Number(v.gc_above_pct) || 80, max_wait_h: Number(v.max_wait_h) || 6,
          requires: v.requires, wake_mode: v.wake_mode, restore_power_state: !!v.restore_power_state };
        if (editing) await api.put("/api/cleanup-policies/" + enc(c.name), body, { suppressToast: true });
        else await api.post("/api/cleanup-policies", Object.assign({ name: v.name.trim() }, body), { suppressToast: true });
        toast((editing ? "Saved " + c.name : "Created " + v.name.trim()), "ok");
        view.refresh();
      },
    });
  }

  // The queue: what runs, what waits, what is held awake.
  let Q = null;
  async function loadQueue() {
    try { Q = await api.get("/api/backup-queue", { suppressToast: true }); } catch (_e) { Q = null; }
    drawQueue();
  }
  async function cancelJob(j) {
    const running = j.state === "running";
    if (!await confirm({ title: running ? "Stop the GC of " + j.name + "?" : "Cancel " + j.name + "?", body: running ? "PBS stops its garbage collection; it starts over next time. The run ends as failed and the queue moves on." : "It is taken out of the queue; it does not run.", action: running ? "Stop the GC" : "Cancel it" })) return;
    try { const r = await api.del("/api/backup-queue/" + j.id, { suppressToast: true }); toast(r.done === "stopping" ? "Stopping the GC" : "Cancelled", "ok"); } catch (e) { toast(e.message, "error"); }
    loadQueue();
  }
  function drawQueue() {
    const box = clear($("#queue"));
    if (!Q) { box.append(el("p", { class: "faint", style: { margin: 0 } }, "The queue could not be read.")); return; }
    if (Q.scheduler_running === false) { box.append(el("p", { class: "faint", style: { margin: 0 } }, "The scheduler is off (SCHEDULER_ENABLED=false): nothing is queued.")); return; }
    if (!Q.jobs.length && !Q.held) { box.append(el("p", { class: "faint", style: { margin: 0 } }, "Nothing running or waiting.")); return; }
    const chips = [];
    if (Q.held) chips.push(el("span", { class: "mono faint", style: { marginRight: "4px" } }, Q.held.requires.join(", ") + (Q.held.nodes.length ? " · " + Q.held.nodes.join(", ") : "")),
      el("span", { class: "pill ok" }, "woken " + whenText(Q.held.since)));
    for (const j of Q.jobs) {
      chips.push(el("span", { class: "faint" }, "→"));
      const label = (j.kind === "backup" ? j.name : j.name) + " · " + (j.state === "running" ? (j.kind === "backup" ? "backing up" : "cleaning") : "waiting");
      chips.push(el("span", { class: "pill " + (j.state === "running" ? "pending" : "off") }, label),
        j.kind === "cleanup" || j.state === "waiting" ? el("button", { type: "button", class: "ghost small", "aria-label": (j.state === "running" ? "Stop " : "Cancel ") + j.name, onclick: () => cancelJob(j) }, j.state === "running" ? "Stop…" : "Cancel…") : null);
    }
    if (Q.held) chips.push(el("span", { class: "faint" }, "→"), el("span", { class: "pill off" }, "put " + (Q.held.nodes.join(", ") || "it") + " back"));
    box.append(el("div", { class: "queue-row" }, chips));
  }

  // The datastore card, and PBS's own jobs that fail while it is offline.
  let DS = null;
  async function loadDatastore() {
    try { DS = await api.get("/api/pbs-datastore", { suppressToast: true }); } catch (_e) { DS = null; }
    drawDatastore();
  }
  function drawDatastore() {
    const box = clear($("#ds"));
    const jobs = clear($("#pbs-jobs"));
    if (V.pbs_href) $("#ds-link").href = V.pbs_href + "?tab=datastores";
    if (!DS) { box.append(el("p", { class: "faint" }, "Not read yet.")); return; }
    $("#h-ds").textContent = "Datastore " + DS.name;
    const last = DS.last_cleanup;
    const gc = last && last.gc ? last.gc : null;
    const fact = (label, value) => el("div", {}, el("div", { class: "faint", style: { fontSize: "12px" } }, label), el("div", {}, value));
    box.append(el("div", { class: "ds-facts" },
      fact("Used", !DS.reachable ? el("span", { class: "faint" }, "offline" + (DS.error ? ": " + DS.error.slice(0, 80) : ""))
        : DS.total_bytes ? Math.round(DS.used_fraction * 100) + "% · " + fmt.bytes(DS.used_bytes) + " of " + fmt.bytes(DS.total_bytes) : el("span", { class: "faint" }, "PBS did not say how big it is")),
      fact("Waits for the next GC", DS.gc && DS.gc.pending_bytes != null ? fmt.bytes(DS.gc.pending_bytes) : gc && gc.pending ? gc.pending : "—"),
      fact("Last GC", gc && gc.status ? [whenText(last.started_at), " · ", gc.status === "ok" ? "removed " + (gc.removed || "?") : gc.status] : "—")));
    if (!DS.cleaned_by.length) box.append(el("p", { class: "faint", style: { margin: "10px 0 0", fontSize: "12.5px" } }, "No cleanup policy cleans it: old snapshots and unused chunks stay until one does."));
    const perms = DS.permissions || {};
    if (perms.modify === false) box.append(el("div", { class: "banner fail", style: { marginTop: "10px" } }, "The orchestrator's PBS token lacks Datastore.Modify on /datastore/" + DS.name + ": it cannot prune snapshots Proxmox VE made, nor run GC. Give the token (not only its user) the DatastoreAdmin role."));
    const own = (DS.own_jobs || []).filter((j) => j.kind === "gc" || j.kind === "prune");
    if (own.length && DS.cleaned_by.length) {
      jobs.append(el("div", { class: "banner warn" }, el("strong", {}, "PBS runs its own " + own.map((j) => j.kind === "gc" ? "GC" : "prune job").join(" and ") + " on " + DS.name + " (" + own.map((j) => j.schedule || "?").join(", ") + "). "),
        "The datastore is only up while the orchestrator wakes it, so " + (own.length > 1 ? "they fail" : "it fails") + ". Turn " + (own.length > 1 ? "them" : "it") + " off in PBS (Datastore → " + DS.name + " → Prune & GC Jobs) and let " + DS.cleaned_by.map((c) => c.policy).join(", ") + " do it. ",
        V.pbs_href ? el("a", { href: V.pbs_href + "?tab=datastores" }, "Open in PBS →") : null));
    }
  }

  // --- last 24 hours and what is left out --------------------------------------------------------
  function drawLast() {
    const box = clear($("#last"));
    const rank = (r) => (r.status === "failed" ? 0 : r.status === "skipped" ? 1 : 2);
    const runs = V.last_runs.slice().sort((a, b) => rank(a) - rank(b) || a.service.localeCompare(b.service));
    if (!runs.length) { box.append(el("p", { class: "muted", style: { margin: "0 0 10px" } }, "Nothing was backed up in the last 24 hours.")); return; }
    const RES = { succeeded: ["ok", "ok"], failed: ["fail", "failed"], running: ["pending", "running"], skipped: ["warn", "skipped"], queued: ["pending", "queued"] };
    for (const r of runs) {
      const [cls, label] = RES[r.status] || ["off", r.status];
      const text = r.status === "failed" || r.status === "skipped" ? r.error || r.status
        : [r.woke.length ? "woke " + r.woke.join(", ") : "", r.size_bytes ? fmt.bytes(r.size_bytes) : "", r.verified ? "verified" : r.status === "succeeded" ? "not verified" : "", r.took_s ? Math.max(1, Math.round(r.took_s / 60)) + " min" : "", fmt.ago(r.started_at)].filter(Boolean).join(" · ");
      box.append(el("div", { class: "event-row run-row" },
        el("span", { class: "pill " + cls }, label),
        el("a", { href: "/services/" + enc(r.service) + "?tab=related", style: { fontWeight: "600" } }, r.service),
        el("span", { class: "run-text" + (r.status === "failed" || r.status === "skipped" ? "" : " faint") }, text),
        r.status === "failed" || r.status === "skipped"
          ? el("button", { type: "button", class: "small", onclick: () => O.bulk("services", "backup", [r.service]).then(() => view.refresh(), () => {}) }, "Retry")
          : el("a", { class: "btn ghost small", href: "/services/" + enc(r.service) + "?tab=related" }, "Open")));
    }
  }
  function drawLeft() {
    const box = clear($("#left"));
    $("#left-card").hidden = !V.excluded.length && !V.unprotected.length;
    if (V.unprotected.length) box.append(el("p", { style: { margin: "0 0 8px" } }, el("span", { class: "pill warn" }, "no policy"), " ",
      V.unprotected.map((s, i) => [i ? ", " : "", el("a", { href: "/services/" + enc(s) + "?tab=configuration" }, s)]),
      el("span", { class: "faint" }, ": not in any policy, so never backed up on a schedule.")));
    if (V.excluded.length) box.append(el("p", { style: { margin: 0 } }, el("span", { class: "pill off" }, "never"), " ",
      V.excluded.map((s, i) => [i ? ", " : "", el("a", { href: "/services/" + enc(s.slug) }, s.slug), s.reason ? el("span", { class: "faint" }, " (" + s.reason + ")") : ""]),
      el("span", { class: "faint" }, ": left out on purpose.")));
  }

  // --- Prune now on a backup policy (BackupsPrune wireframe) -----------------------------------------
  function pruneDrawer(p) {
    let preview = null;
    const list = el("div", { class: "prune-list" }, el("p", { class: "faint" }, "Asking PBS what it would remove (its own dry run)…"));
    const d = drawer({
      title: p.name + ": keeps " + keepsText(p.retention),
      sub: "Policies → " + p.name + " → Prune now",
      values: { gc: true },
      saveLabel: "Prune",
      note: "Audited as you. Protected and newest verified backups are never removed. Queued: it wakes " + (p.requires.join(", ") || "nothing") + " and puts it back.",
      fields: [
        { type: "custom", key: "_list", render: () => list },
        { key: "gc", label: "Run garbage collection after pruning", type: "checkbox", help: "Pruning alone frees nothing: the space comes back when PBS's garbage collection has run. The run waits for it." },
      ],
      async onSave(v) {
        await api.post("/api/backup-policies/" + enc(p.name) + "/prune", { gc: !!v.gc }, { suppressToast: true });
        toast("Queued: prune " + p.name + (v.gc ? " and garbage collection" : "") + ". Follow it on the Cleanup tab", "ok");
      },
    });
    const save = d.el.querySelector('button[type="submit"]');
    if (save) save.classList.add("danger-solid"); // it deletes backups (BackupsPrune wireframe)
    api.post("/api/backup-policies/" + enc(p.name) + "/prune-preview", {}, { suppressToast: true }).then((r) => {
      preview = r;
      const anyError = r.services.some((x) => x.error);
      fill(list,
        el("div", { class: "tags", style: { marginBottom: "10px" } },
          el("span", { class: "pill " + (r.total ? "warn" : "off") }, r.total + " snapshot" + (r.total === 1 ? "" : "s") + " to remove"),
          p.retention && p.retention.protect_latest_verified ? el("span", { class: "pill ok" }, "newest verified of each kept (protected)") : null),
        anyError && r.services.every((x) => x.error) ? el("div", { class: "banner warn" }, "PBS could not be asked (the datastore is probably offline). Prune still works: it is queued and wakes the storage first.") : null,
        r.services.map((x) => el("div", { class: "prune-svc" },
          el("div", { class: "prune-head" }, el("strong", {}, x.service), el("span", { class: "mono faint" }, x.group),
            el("span", { class: "pill " + (x.error ? "fail" : x.would_remove.length ? "warn" : "off"), style: { marginLeft: "auto" } },
              x.error ? "PBS refused" : x.would_remove.length ? x.would_remove.length + " to remove" : x.skipped ? "keeps everything" : "nothing to remove")),
          x.error ? el("div", { class: "prune-row faint" }, x.error)
            : x.would_remove.slice(0, 4).map((sid) => el("div", { class: "prune-row mono" }, sid.split("/").slice(2).join("/"))),
          !x.error && x.would_remove.length > 4 ? el("div", { class: "prune-row faint" }, "and " + (x.would_remove.length - 4) + " older") : null)));
      if (save) save.textContent = r.total ? "Prune " + r.total + " snapshot" + (r.total === 1 ? "" : "s") : "Prune";
    }, (e) => fill(list, el("div", { class: "banner warn" }, "Could not preview: " + e.message + ". Prune still works: it is queued and wakes the storage first.")));
    void preview;
  }

  // --- deleting snapshots by hand (BackupsDelete wireframe) ---------------------------------------
  async function deleteSnapshots(rows) {
    const ok = rows.filter((s) => !s.keep_reason);
    const kept = rows.filter((s) => s.keep_reason);
    if (!ok.length) { toast("Nothing to delete: " + kept.map((s) => s.keep_reason).join("; "), "error"); return; }
    const services = Array.from(new Set(ok.map((s) => s.service || s.group)));
    const name = services.length === 1 ? services[0] : String(ok.length);
    const others = (s) => (snapTable_rows || []).filter((x) => x.group === s.group && !ok.includes(x)).length;
    const body = el("div", {},
      el("p", {}, ok.length === 1 ? "It is removed from PBS for good. " + (ok[0].service || ok[0].group) + " keeps " + others(ok[0]) + " other backup" + (others(ok[0]) === 1 ? "" : "s") + "." : "They are removed from PBS for good."),
      el("ul", { class: "affected" }, ok.map((s) => el("li", { class: "mono" }, s.snapshot_id + (s.size_bytes ? " · " + fmt.bytes(s.size_bytes) : "") + " · " + (s.verified ? "verified" : "not verified") + (s.made_by ? " · made by " + s.made_by : "")))),
      el("div", { class: "banner warn" }, "Never offered: a ", el("strong", {}, "protected"), " snapshot, or the ", el("strong", {}, "newest verified"), " one of a service. Unprotect it in PBS first if you really mean it."),
      kept.length ? el("p", { class: "faint" }, kept.length + " selected kept: " + kept.map((s) => s.snapshot_id + " (" + s.keep_reason + ")").join("; ")) : null,
      el("p", { class: "faint", style: { fontSize: "12px" } }, "Audited as you. Space comes back after PBS's garbage collection."));
    if (!await confirm({ title: "Delete " + ok.length + " snapshot" + (ok.length === 1 ? "" : "s") + (services.length === 1 ? " of " + services[0] : "") + "?", body, typeName: name, action: ok.length === 1 ? "Delete snapshot" : "Delete " + ok.length + " snapshots" })) return;
    try {
      const r = await api.post("/api/infra/pbs/snapshots/delete", { snapshot_ids: ok.map((s) => s.snapshot_id) }, { suppressToast: true });
      const refused = Object.keys(r.refused || {});
      toast("Deleted " + r.deleted.length + (refused.length ? "; kept " + refused.length + ": " + Object.values(r.refused).join("; ") : ""), refused.length ? "error" : "ok");
    } catch (e) { toast(e.message, "error"); }
    snapTable.clearSelection();
    loadSnapshots();
  }
  let snapTable_rows = [];

  // --- snapshots -----------------------------------------------------------------------------
  const snapAge = el("span", { class: "age" });
  const snapTable = table($("#snapshots"), {
    rows: [], key: (s) => s.snapshot_id, noun: ["snapshot", "snapshots"], urlState: false, placeholder: "Filter by ID, owner…", age: snapAge,
    search: (s) => [s.snapshot_id, s.group, s.owner].join(" "),
    sort: { key: "when", dir: -1 },
    columns: [
      { key: "id", label: "Snapshot", sort: (s) => s.snapshot_id, render: (s) => el("span", { class: "mono", style: { overflowWrap: "anywhere" } }, s.snapshot_id) },
      { key: "when", label: "Taken", sort: (s) => s.backup_time, render: (s) => { const iso = typeof s.backup_time === "number" ? new Date(s.backup_time * 1000).toISOString() : s.backup_time; return [fmt.time(iso), el("span", { class: "sub" }, fmt.ago(iso))]; } },
      { key: "size", label: "Size", sort: (s) => s.size_bytes || 0, cls: "num", render: (s) => s.size_bytes == null ? "—" : fmt.bytes(s.size_bytes) },
      { key: "checked", label: "Checked", sort: (s) => (s.verified ? 0 : 1), render: (s) => [el("span", { class: "pill " + (s.verified ? "ok" : "off") }, s.verified ? "verified" : "not verified"), s.protected ? el("span", { class: "tag", style: { marginLeft: "6px" } }, "protected") : null, s.newest_verified ? el("span", { class: "tag", style: { marginLeft: "6px" } }, "newest verified") : null] },
      { key: "service", label: "Service", sort: (s) => s.service || "", render: (s) => s.service ? el("a", { href: "/services/" + enc(s.service) + "?tab=related" }, s.service) : el("span", { class: "faint" }, "no service") },
      { key: "made", label: "Made by", sort: (s) => s.made_by || "", render: (s) => el("span", { class: "faint" }, s.made_by || "not by the orchestrator") },
    ],
    bulk: (rows) => [{ label: "Delete selected…", danger: true, onClick: () => deleteSnapshots(rows) }],
    menu: (s) => [
      s.service ? { label: "Open " + s.service, href: "/services/" + enc(s.service) + "?tab=related" } : null,
      "-",
      s.keep_reason ? { label: "Kept: " + s.keep_reason, disabled: true } : { label: "Delete…", danger: true, onClick: () => deleteSnapshots([s]) },
    ].filter(Boolean),
    chips: [
      { key: "vm", label: "VMs", test: (s) => /^vm\//.test(s.snapshot_id) },
      { key: "ct", label: "Containers", test: (s) => /^ct\//.test(s.snapshot_id) },
      { key: "host", label: "Hosts", test: (s) => /^host\//.test(s.snapshot_id) },
      { key: "unverified", label: "Not verified", test: (s) => !s.verified },
      { key: "protected", label: "Protected", test: (s) => s.protected },
    ],
    empty: { title: "No snapshots", text: "The datastore has none, or PBS cannot be reached: see Settings → Connections." },
  });
  let snaps = null;
  function loadSnapshots() {
    if (snaps) return snaps.refresh();
    snaps = region(el("div"), { load: () => api.get("/api/infra/pbs/snapshots", { suppressToast: true }), render: (l) => { snapTable_rows = l; snapTable.setRows(l); }, topics: ["run"], filter: (d) => (d.kind === "backup" || d.kind === "cleanup") && d.status !== "running", wait: 5000, every: 300000, age: snapAge });
  }

  // --- dumps ---------------------------------------------------------------------------------------
  const dumpTable = table($("#dumps"), {
    rows: [], key: (d) => d.service, noun: ["service", "services"], urlState: false, placeholder: "Filter",
    sort: { key: "service", dir: 1 },
    columns: [
      { key: "service", label: "Service", sort: true, render: (d) => [el("a", { href: "/services/" + enc(d.service) + "?tab=related#dumps", style: { fontWeight: "600" } }, d.name || d.service), el("span", { class: "sub mono" }, d.service)] },
      { key: "type", label: "Database", sort: true },
      { key: "schedule", label: "When", render: (d) => d.schedule ? cronText(d.schedule) : "by hand only" },
      { key: "kept", label: "Kept", sort: (d) => d.total_bytes, render: (d) => d.count + " · " + fmt.bytes(d.total_bytes) },
      { key: "latest", label: "Newest good one", sort: (d) => (d.latest ? d.latest.created_at : ""), render: (d) => d.latest ? [fmt.time(d.latest.created_at), el("span", { class: "sub" }, fmt.ago(d.latest.created_at))] : el("span", { class: "pill warn" }, "none yet") },
      { key: "enabled", label: "", render: (d) => d.enabled ? "" : el("span", { class: "pill off" }, "off") },
    ],
    empty: { title: "No dumps set up", text: "Dump a service's database on its own from the service's Related tab." },
  });
  let dumps = null;
  function loadDumps() { if (dumps) return dumps.refresh(); dumps = region(el("div"), { load: () => api.get("/api/dumps", { suppressToast: true }), render: (l) => dumpTable.setRows(l), topics: ["run", "registry"], wait: 3000 }); }

  // --- orchestrator backups --------------------------------------------------------------------------
  const selfTable = table($("#self"), {
    rows: [], key: (b) => b.name, noun: ["backup", "backups"], urlState: false, placeholder: "Filter",
    sort: { key: "created", dir: -1 },
    columns: [
      { key: "created", label: "Taken", sort: (b) => b.created_at, render: (b) => [fmt.time(b.created_at), el("span", { class: "sub" }, fmt.ago(b.created_at))] },
      { key: "reason", label: "Why", sort: true, render: (b) => b.reason || "—" },
      { key: "size", label: "Size", sort: (b) => b.size_bytes || 0, cls: "num", render: (b) => fmt.bytes(b.size_bytes) },
      { key: "version", label: "Version", render: (b) => el("span", { class: "mono" }, (b.version || "—") + (b.git_sha ? " " + b.git_sha.slice(0, 7) : "")) },
      { key: "note", label: "Note", render: (b) => b.note || "" },
    ],
    menu: (b) => [
      { label: "Download", href: "/api/system/backups/" + enc(b.name) + "/download" },
      { label: "Restore…", onClick: () => restoreSelf(b) },
      "-",
      { label: "Delete…", danger: true, onClick: async () => { if (await confirm({ title: "Delete " + b.name + "?", action: "Delete" })) { try { await api.del("/api/system/backups/" + enc(b.name)); loadSelf(); } catch (_e) { /* toast */ } } } },
    ],
    empty: { title: "No backups of the orchestrator yet" },
  });
  async function restoreSelf(b) {
    if (!await confirm({ title: "Restore " + b.name + "?", body: "It replaces the database" + (b.has_registry ? " and services.yaml" : "") + " when the orchestrator restarts. Anything newer is lost; the current state is backed up first.", typeName: "restore", action: "Stage the restore" })) return;
    try { await api.post("/api/system/backups/" + enc(b.name) + "/restore", {}, { suppressToast: true }); toast("Restore staged: restart to apply it", "ok"); } catch (e) { toast(e.message, "error"); }
    loadSelf();
  }
  async function waitForRestart() {
    toast("Restarting… this page comes back by itself");
    await new Promise((r) => setTimeout(r, 3000));
    for (let i = 0; i < 90; i++) {
      try { const r = await fetch("/healthz", { cache: "no-store" }); if (r.ok) { location.href = "/backup-policies?tab=orchestrator"; return; } } catch (_e) { /* still down */ }
      await new Promise((r) => setTimeout(r, 2000));
    }
    toast("The orchestrator did not come back within 3 minutes: check the container", "error");
  }
  async function loadSelf() {
    try { selfTable.setRows(await api.get("/api/system/backups", { suppressToast: true })); } catch (_e) { /* toast */ }
    let info = null;
    try { info = await api.get("/api/system/info", { suppressToast: true }); } catch (_e) { /* no banner */ }
    const p = info && info.pending_restore;
    const banner = $("#restore-banner");
    banner.hidden = !p;
    if (p) fill(banner, el("strong", {}, "A restore is staged. "), "Restoring " + p.backup + " replaces the database" + (p.has_registry ? " and services.yaml" : "") + " when the orchestrator restarts. What is there now is backed up first. ",
      el("button", { type: "button", class: "danger-solid small", onclick: async () => { if (await confirm({ title: "Restart now and restore?", action: "Restart and restore" })) { try { await api.post("/api/system/restart", {}); waitForRestart(); } catch (_e) { /* toast */ } } } }, "Restart and restore"), " ",
      el("button", { type: "button", class: "secondary small", onclick: async () => { try { await api.del("/api/system/restore"); loadSelf(); } catch (_e) { /* toast */ } } }, "Cancel"));
  }
  $("#self-backup").addEventListener("click", () => drawer({
    title: "Back up the orchestrator", values: { note: "" }, saveLabel: "Back up now",
    fields: [{ key: "note", label: "Note", placeholder: "optional: why" }],
    async onSave(v) { const b = await api.post("/api/system/backups", { note: v.note || "" }, { suppressToast: true }); toast("Saved " + b.name + " (" + fmt.bytes(b.size_bytes) + ")", "ok"); loadSelf(); },
  }));

  // --- sections, tabs ------------------------------------------------------------------------------
  const view = region(el("div"), {
    initial: V,
    load: () => api.get("/api/views/backups", { suppressToast: true }),
    render: (d) => { V = d; policyTable.setRows(d.policies); cleanupTable.setRows(d.cleanups || []); drawTiles(); drawLast(); drawLeft(); },
    topics: ["run", "registry"], filter: (d) => !d.kind || d.kind === "backup" || d.kind === "cleanup", wait: 2000,
  });
  if (window.OrchLive) window.OrchLive.on(["run"], () => { if (tabsCtl.current === "cleanup") { loadQueue(); loadDatastore(); } }, { wait: 1500 });
  setInterval(() => { if (!document.hidden && tabsCtl.current === "cleanup") loadQueue(); }, 10000);
  const loaded = {};
  const newBtn = $("#new-policy");
  const tabsCtl = tabs(document.querySelector('[role="tablist"]'), { onChange: (t) => {
    newBtn.textContent = t === "cleanup" ? "+ New cleanup policy" : "+ New policy";
    if (t === "cleanup") { loadQueue(); loadDatastore(); }
    if (loaded[t]) return; loaded[t] = true;
    if (t === "snapshots") setTimeout(loadSnapshots, 0);
    if (t === "dumps") setTimeout(loadDumps, 0);
    if (t === "orchestrator") setTimeout(loadSelf, 0);
  } });
  api.get("/api/infra/pbs/datastore", { suppressToast: true }).then((d) => { datastore = d; drawTiles(); }, () => { datastore = { name: "", reachable: false }; drawTiles(); });
  newBtn.addEventListener("click", () => (tabsCtl.current === "cleanup" ? cleanupDrawer(null) : policyDrawer(null)));
  const q = new URLSearchParams(location.search);
  if (q.get("new") === "1") policyDrawer(null);
  if (q.get("edit")) { const p = V.policies.find((x) => x.name === q.get("edit")); if (p) policyDrawer(p); }
  void view;
})();
