/* Services: the list of everything the orchestrator manages (templates/services.html).
 * Data: GET /api/views/services, refreshed in place on health, run and registry events. */
(function () {
  "use strict";
  const { $, el, api, fmt, region, table, drawer } = window.UI;
  const { bulk, serviceActions, serviceDrawer } = window.Objects;
  let view = JSON.parse($("#initial").textContent);
  const age = el("span", { class: "age" });

  const two = (main, sub, href) => [href ? el("a", { href }, main) : el("span", {}, main), sub ? el("span", { class: "sub" }, sub) : null];
  const t = table($("#services-table"), {
    rows: view.rows,
    key: (r) => r.slug,
    name: (r) => r.name,
    noun: ["service", "services"],
    placeholder: "Filter by name, slug, node, group…",
    search: (r) => [r.name, r.slug, r.node, r.group, r.parent, r.runs_on.main, r.state.label].join(" "),
    sort: { key: "name", dir: 1 },
    children: (r) => r.parent,
    rowClass: (r) => (r.parent ? "child" : ""),
    age,
    columns: [
      { key: "name", label: "Service", sort: (r) => r.name.toLowerCase(),
        render: (r) => [el("a", { href: "/services/" + encodeURIComponent(r.slug), style: { fontWeight: "600", color: "var(--text)" } }, r.name), el("span", { class: "sub mono" }, r.slug)] },
      { key: "state", label: "State", phone: false, sort: (r) => ["fail", "warn", "pending", "off", "ok"].indexOf(r.state.cls),
        render: (r) => el("span", { class: "pill " + r.state.cls, title: r.state.why || null }, r.state.label) },
      { key: "runs_on", label: "Runs on", sort: (r) => r.runs_on.main, render: (r) => two(r.runs_on.main, r.runs_on.sub, r.runs_on.href) },
      { key: "check", label: "Last check", render: (r) => two(r.check.text, r.check.at ? fmt.ago(r.check.at) : "") },
      { key: "backup", label: "Backup", sort: (r) => r.backup.main,
        render: (r) => two(r.backup.main, [r.backup.sub, r.backup.at ? fmt.ago(r.backup.at) : ""].filter(Boolean).join(" · ")) },
      { key: "wake_on_hit", label: "Wake on hit", sort: true, render: (r) => el("span", { title: r.wake_on_hit_why || null }, r.wake_on_hit) },
    ],
    chips: [
      { key: "failed", label: "Failed", test: (r) => r.state.cls === "fail" },
      { key: "warn", label: "Warnings", test: (r) => r.state.cls === "warn" },
      { key: "off", label: "Off", test: (r) => r.state.cls === "off" },
      { key: "unbacked", label: "Not backed up", test: (r) => r.not_backed_up },
    ],
    groupBy: [
      { key: "group", label: "Group", of: (r) => r.group, none: "No group" },
      { key: "node", label: "Node", of: (r) => r.node, none: "No node" },
      { key: "state", label: "State", of: (r) => r.state.label },
    ],
    menu: (r) => serviceActions(r, view, refresh),
    bulk: (rows) => {
      const slugs = rows.map((r) => r.slug);
      const run = (action, params) => () => bulk("services", action, slugs, params).then(() => { t.clearSelection(); refresh(); }, () => {});
      return [
        { label: "Check", onClick: run("resync") },
        { label: "Wake", onClick: run("wake") },
        { label: "Set backup policy…", onClick: () => policyDrawer(slugs) },
        { label: "Find addresses", onClick: run("discover_ip") },
        { label: "Enable", onClick: run("enable") },
        { label: "Disable", onClick: run("disable") },
      ];
    },
    empty: {
      title: "No services yet",
      text: "Import your Proxmox guests, or add a service by hand.",
      action: el("a", { class: "btn small", href: "/nodes" }, "Import from Proxmox"),
    },
  });

  function policyDrawer(slugs) {
    drawer({
      title: "Set the backup policy",
      sub: slugs.length + " service" + (slugs.length === 1 ? "" : "s") + ": " + slugs.slice(0, 4).join(", ") + (slugs.length > 4 ? "…" : ""),
      values: { policy: "" },
      saveLabel: "Set policy",
      fields: [{ key: "policy", label: "Backup policy", type: "select", options: [{ value: "", label: "None: not backed up" }].concat(view.policies.map((p) => ({ value: String(p.id), label: p.name }))) }],
      async onSave(v) {
        await bulk("services", "update", slugs, { backup_policy_id: v.policy === "" ? null : Number(v.policy) });
        t.clearSelection(); refresh();
      },
    });
  }

  const r = region($("#services-table"), {
    initial: view,
    load: () => api.get("/api/views/services", { suppressToast: true }),
    render: (data) => { view = data; t.setRows(data.rows); $("#svc-count").textContent = data.rows.length; },
    topics: ["health", "run", "registry"],
    wait: 1200,
    age,
  });
  function refresh() { r.refresh(); }

  $("#new-service").addEventListener("click", () => serviceDrawer({ view, onSaved: refresh }));
  if (new URLSearchParams(location.search).get("new") === "1") serviceDrawer({ view, onSaved: refresh });
})();
