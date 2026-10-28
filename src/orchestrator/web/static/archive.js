/* The archive (templates/archive.html), to the Archive wireframe: what was deleted, what comes
 * back with it, how long it stays; restore or delete for good, one at a time or selected.
 * Data: embedded at first paint, then GET /api/archive on registry and audit events. */
(function () {
  "use strict";
  const { $, el, api, fmt, region, table, confirm } = window.UI;
  const toast = (m, k) => window.toast(m, k);
  const KIND = { service: "service", proxy_host: "proxy host", backup_policy: "policy" };

  async function restore(rows) {
    const done = []; const notes = [];
    for (const r of rows) {
      try {
        const out = await api.post("/api/archive/" + r.id + "/restore", undefined, { suppressToast: true });
        done.push(...out.restored); notes.push(...out.warnings);
      } catch (e) { toast((r.name || r.slug) + ": " + e.message, "error"); }
    }
    if (done.length) toast("Restored " + done.join(", ") + (notes.length ? ". Note: " + notes.join("; ") : ""), notes.length ? "" : "ok");
    t.clearSelection(); view.refresh();
  }
  async function purge(rows) {
    const names = rows.map((r) => r.name || r.slug);
    const ok = await confirm({
      title: rows.length === 1 ? "Delete " + names[0] + " for good?" : "Delete " + rows.length + " items for good?",
      body: "They cannot be restored afterwards.",
      affectedLabel: rows.length > 1 ? "Deleted for good" : null, affected: rows.length > 1 ? names : null,
      typeName: rows.length === 1 ? rows[0].slug : null, action: "Delete for good",
    });
    if (!ok) return;
    for (const r of rows) {
      try { await api.del("/api/archive/" + r.id, { suppressToast: true }); } catch (e) { toast(e.message, "error"); }
    }
    t.clearSelection(); view.refresh();
  }

  const initial = JSON.parse($("#initial").textContent);
  const age = el("span", { class: "age" });
  const t = table($("#archive-table"), {
    rows: initial,
    key: (r) => r.id,
    name: (r) => r.name || r.slug,
    noun: ["item", "items"],
    placeholder: "Filter by name…",
    search: (r) => [r.name, r.slug, r.kind_label, r.summary, r.deleted_by].join(" "),
    sort: { key: "deleted", dir: -1 },
    age,
    columns: [
      { key: "what", label: "What", sort: (r) => (r.name || r.slug).toLowerCase(),
        render: (r) => [el("span", { class: "tag" }, KIND[r.kind] || r.kind_label), " ",
          el("strong", { class: r.kind === "proxy_host" ? "mono" : null }, r.name || r.slug),
          r.name && r.name !== r.slug && r.kind !== "proxy_host" ? el("span", { class: "sub mono" }, r.slug) : null] },
      { key: "deleted", label: "Deleted", sort: (r) => r.deleted_at, render: (r) => el("time", { datetime: r.deleted_at, title: fmt.time(r.deleted_at) }, fmt.ago(r.deleted_at)) },
      { key: "by", label: "By", sort: (r) => r.deleted_by || "", render: (r) => r.deleted_by || "—" },
      { key: "contents", label: "Comes back with", phone: false, render: (r) => el("span", { class: "faint", style: { fontSize: "13px" } }, r.summary || "only itself") },
      { key: "left", label: "Gone for good in", sort: (r) => r.days_left,
        render: (r) => r.days_left <= 3 ? el("span", { class: "pill warn" }, r.days_left + (r.days_left === 1 ? " day" : " days")) : r.days_left + " days" },
      { key: "restore", label: "", render: (r) => el("button", { type: "button", class: "secondary small", onclick: (e) => { e.stopPropagation(); restore([r]); } }, "Restore") },
    ],
    chips: [
      { key: "service", label: "Services", test: (r) => r.kind === "service" },
      { key: "proxy_host", label: "Proxy hosts", test: (r) => r.kind === "proxy_host" },
      { key: "backup_policy", label: "Policies", test: (r) => r.kind === "backup_policy" },
    ],
    menu: (r) => [
      { label: "Restore", onClick: () => restore([r]) },
      "-",
      { label: "Delete for good…", danger: true, onClick: () => purge([r]) },
    ],
    bulk: (rows) => [
      { label: "Restore", onClick: () => restore(rows) },
      { label: "Delete for good…", danger: true, onClick: () => purge(rows) },
    ],
    empty: { title: "Nothing deleted lately", text: "A deleted service, proxy host or backup policy waits here and can be restored." },
  });
  const view = region($("#archive-table"), {
    initial,
    load: () => api.get("/api/archive", { suppressToast: true }),
    render: (rows) => t.setRows(rows),
    topics: ["registry", "audit"], wait: 1200, age,
  });
})();
