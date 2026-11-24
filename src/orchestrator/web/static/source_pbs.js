/* A PBS source's Datastores tab (SourcePbsDatastores wireframe; docs/cleanup.md): each datastore's
 * usage, PBS's last GC and which cleanup policy cleans it, then for the chosen one PBS's own GC,
 * prune and verify jobs (read from PBS, changed in PBS) and what the orchestrator's token may do.
 * Data: the source's datastores resource, and GET /api/pbs-datastore?name= per datastore. */
(function () {
  "use strict";
  const { el, fill, clear, api, fmt, table, region } = window.UI;
  const enc = encodeURIComponent;
  window.SourceViews = window.SourceViews || {};

  const KIND = { gc: "Garbage collection", prune: "Prune job", verify: "Verify job" };
  const epoch = (v) => (v ? new Date(v * 1000).toISOString() : null);

  window.SourceViews["pbs.datastores"] = function (panel, ctx) {
    const tableBox = el("div");
    const below = el("div", { class: "cleanup-grid" });
    fill(panel, tableBox, below);
    const info = {};
    let chosen = null;

    const t = table(tableBox, {
      rows: [], key: (r) => r.id, noun: ["datastore", "datastores"], urlState: false, plain: true,
      columns: [
        { key: "name", label: "Datastore", sort: true, render: (r) => [el("button", { type: "button", class: "ghost", style: { padding: 0, height: "auto", color: "var(--text)", fontWeight: "600" }, onclick: () => choose(r.name) }, r.name),
          info[r.name] && !info[r.name].reachable ? el("span", { class: "sub" }, "offline: " + (info[r.name].error || "").slice(0, 80)) : null] },
        { key: "pct", label: "Used", sort: (r) => r.pct || 0, render: (r) => r.total ? [r.pct + "% · " + fmt.bytes(r.used) + " of " + fmt.bytes(r.total),
          el("div", { class: "bar", role: "img", "aria-label": r.pct + "% used" }, el("span", { class: r.pct >= 95 ? "fail" : r.pct >= 85 ? "warn" : "ok", style: { width: r.pct + "%" } }))] : el("span", { class: "faint" }, "—") },
        { key: "gc", label: "Last GC (from PBS)", render: (r) => {
          const d = info[r.name];
          if (!d) return el("span", { class: "faint" }, "…");
          const job = (d.own_jobs || []).find((j) => j.kind === "gc");
          const last = d.last_cleanup && d.last_cleanup.gc && d.last_cleanup.gc.status ? d.last_cleanup : null;
          if (job && job.last_state) return [el("span", { class: "pill " + (job.last_state === "ok" ? "ok" : "fail") }, (job.last_state === "ok" ? "ok" : "failed") + (job.last_end ? " · " + fmt.ago(epoch(job.last_end)) : "")),
            job.last_state !== "ok" ? el("span", { class: "sub", style: { overflowWrap: "anywhere" } }, String(job.last_state).slice(0, 120)) : null];
          if (last) return [el("span", { class: "pill " + (last.gc.status === "ok" ? "ok" : last.gc.status === "skipped" ? "off" : "fail") }, last.gc.status + " · " + fmt.ago(last.started_at)), last.gc.removed ? el("span", { class: "sub" }, "removed " + last.gc.removed) : null];
          return el("span", { class: "faint" }, "unknown");
        } },
        { key: "cleaned", label: "Cleaned by", render: (r) => {
          const d = info[r.name];
          if (!d) return el("span", { class: "faint" }, "…");
          if (!d.cleaned_by.length) return [el("span", { class: "pill warn" }, "no cleanup policy"), el("span", { class: "sub" }, el("a", { href: "/backup-policies?tab=cleanup" }, "Set one up"))];
          return d.cleaned_by.map((c) => el("div", {}, el("a", { href: "/backup-policies?tab=cleanup" }, c.policy),
            el("span", { class: "sub" }, c.used_by.map((u) => u.when + " " + u.policy).concat(c.schedule_cron ? [c.schedule_cron] : []).join(" · ") || "by hand only")));
        } },
      ],
      empty: { title: "No datastores", text: "PBS lists none, or cannot be reached." },
    });

    function choose(name) { chosen = name; drawBelow(); }
    function drawBelow() {
      clear(below);
      const d = info[chosen];
      if (!d) return;
      const jobs = d.own_jobs || [];
      // The orchestrator cleans it (so it is likely woken only for that), or it is offline now.
      const offlineOnly = d.cleaned_by.length > 0 || !d.reachable;
      below.append(el("section", { class: "card flush" },
        el("div", { class: "card-head" }, el("h2", {}, chosen + " · PBS's own jobs"), el("span", { style: { flex: 1 } }), el("span", { class: "faint", style: { fontSize: "12px" } }, "read from PBS · change them in PBS")),
        jobs.length ? el("div", {}, jobs.map((j) => el("div", { class: "event-row", style: { gridTemplateColumns: "minmax(0,1.3fr) minmax(0,1fr) minmax(0,1fr)", padding: "10px 18px" } },
          el("span", {}, KIND[j.kind] || j.kind, j.kind !== "gc" ? el("span", { class: "mono faint" }, " " + j.id) : null),
          el("span", { class: "mono" }, (j.schedule || "no schedule") + (j.detail ? " · " + j.detail : "")),
          el("span", {}, j.last_state ? el("span", { class: "pill " + (j.last_state === "ok" ? "ok" : "fail") }, j.last_state === "ok" ? "ok" : "failed") : el("span", { class: "pill off" }, "never ran"),
            j.last_end ? el("span", { class: "sub" }, fmt.ago(epoch(j.last_end))) : null))))
          : el("p", { class: "faint", style: { padding: "0 18px" } }, d.own_jobs_error ? "Could not read them: " + d.own_jobs_error : "None, or PBS did not say."),
        jobs.some((j) => j.kind === "gc" || j.kind === "prune") && offlineOnly
          ? el("div", { class: "banner warn", style: { margin: "12px 16px" } }, "These run at fixed times; if " + chosen + " is only online while the orchestrator wakes it, they fail. Turn off GC and prune here and leave them to " + (d.cleaned_by.map((c) => c.policy).join(", ") || "a cleanup policy") + ". A prune job here also ignores the backup policies' keep rules.") : null));
      const perms = d.permissions || {};
      const permRow = (ok, text) => el("div", { style: { display: "flex", gap: "8px", alignItems: "center" } }, el("span", { class: "pill " + (ok === true ? "ok" : ok === false ? "fail" : "off") }, ok === true ? "ok" : ok === false ? "missing" : "unknown"), text);
      below.append(el("section", { class: "card" },
        el("div", { class: "card-head" }, el("h2", {}, "What the orchestrator needs on PBS")),
        perms.error ? el("p", { class: "faint" }, "Could not read the token's permissions: " + perms.error) : [
          permRow(perms.audit, "Datastore.Audit · list and verify"),
          permRow(perms.modify, "Datastore.Modify on /datastore/" + chosen + " · prune any group, run GC")],
        el("p", { class: "faint", style: { fontSize: "12.5px" } }, "Checked with the token's own permissions. Datastore.Prune alone only covers groups the token owns; snapshots Proxmox VE made belong to its own token."),
        el("div", { style: { borderTop: "1px solid var(--line)", paddingTop: "10px", fontSize: "13px" } }, el("div", { class: "faint", style: { fontSize: "12px" } }, "The datastore needs room to clean"),
          "GC writes a little before it frees anything. Keep the storage's quota a few GB above what is used, or GC fails with \"Quota exceeded\".")));
    }

    region(el("div"), {
      every: 60000,
      load: ctx.load,
      async render(rows) {
        t.setRows(rows);
        await Promise.all(rows.map((r) => api.get("/api/pbs-datastore?name=" + enc(r.name), { suppressToast: true }).then((d) => { info[r.name] = d; }, () => {})));
        t.setRows(rows);
        if (!chosen && rows.length) chosen = rows[0].name;
        drawBelow();
      },
    });
  };
})();
