/* Services: the list of everything the orchestrator manages (templates/services.html).
 * Data: GET /api/views/services, refreshed in place on health, run and registry events. */
(function () {
  "use strict";
  const { $, el, api, fmt, region, table, tabs, drawer } = window.UI;
  const { bulk, serviceActions, serviceDrawer, subList } = window.Objects;
  let view = JSON.parse($("#initial").textContent);
  const age = el("span", { class: "age" });
  // A sub-service is not a row of its own: its parent's state takes it in, and ▸ opens it.
  const open = new Set();
  let bySlug = {};
  const tops = (rows) => { bySlug = Object.fromEntries(rows.map((r) => [r.slug, r])); return rows.filter((r) => !r.parent); };
  const below = (r) => r.subs.flatMap((x) => [bySlug[x], ...below(bySlug[x])]).filter(Boolean);
  const brief = (x) => ({ slug: x.slug, name: x.name, cls: x.state.cls, label: x.state.label, kind: x.kind, containers: x.containers, stack: x.stack, depth: x.depth });
  function toggle(r) { if (open.has(r.slug)) open.delete(r.slug); else open.add(r.slug); t.render(); }

  const two = (main, sub, href) => [href ? el("a", { href }, main) : el("span", {}, main), sub ? el("span", { class: "sub" }, sub) : null];
  const t = table($("#services-table"), {
    rows: tops(view.rows),
    key: (r) => r.slug,
    name: (r) => r.name,
    noun: ["service", "services"],
    placeholder: "Filter by name, slug, node, group…",
    search: (r) => [r.name, r.slug, r.node, r.group, r.runs_on.main, r.state.label, ...below(r).map((x) => x.name + " " + x.slug)].join(" "),
    sort: { key: "name", dir: 1 },
    age,
    detail: (r) => (open.has(r.slug) && r.subs.length ? subList(below(r).map(brief)) : null),
    columns: [
      { key: "name", label: "Service", sort: (r) => r.name.toLowerCase(),
        render: (r) => [el("span", {}, r.subs.length ? el("button", { type: "button", class: "sub-toggle", "aria-expanded": String(open.has(r.slug)), "aria-label": (open.has(r.slug) ? "Hide" : "Show") + " the sub-services of " + r.name, onclick: () => toggle(r) }, open.has(r.slug) ? "▾" : "▸") : null,
          el("a", { href: "/services/" + encodeURIComponent(r.slug), style: { fontWeight: "600", color: "var(--text)" } }, r.name),
          r.subs.length ? el("span", { class: "sub-count", title: below(r).length + " sub-service" + (below(r).length === 1 ? "" : "s") }, String(below(r).length)) : null),
          el("span", { class: "sub mono" }, r.slug)] },
      { key: "state", label: "State", phone: false, sort: (r) => ["fail", "warn", "pending", "off", "ok"].indexOf(r.state.cls),
        render: (r) => [el("span", { class: "pill " + r.state.cls, title: r.state.why || null }, r.state.label), r.sub_failing.length ? el("span", { class: "sub" }, r.sub_failing.slice(0, 2).join(", ") + " failing") : null] },
      { key: "runs_on", label: "Runs on", sort: (r) => r.runs_on.main, render: (r) => two(r.runs_on.main, r.runs_on.sub, r.runs_on.href) },
      { key: "check", label: "Last check", render: (r) => two(r.check.text, r.check.at ? fmt.ago(r.check.at) : "") },
      { key: "backup", label: "Backup", sort: (r) => r.backup.main,
        render: (r) => { const c = two(r.backup.main, [r.backup.sub, r.backup.at ? fmt.ago(r.backup.at) : ""].filter(Boolean).join(" · ")); if (r.backup.why && c[1]) c[1].title = r.backup.why; return c; } },
      { key: "wake_on_hit", label: "Wake on hit", sort: true, render: (r) => two(r.wake_on_hit, r.wake_on_hit_why) },
    ],
    chips: [
      { key: "failed", label: "Failed", test: (r) => r.state.cls === "fail" || r.sub_failing.length > 0 },
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

  // Every sub-service in one list, with what it is part of and its stack.
  const subsOf = (rows) => rows.filter((r) => r.parent);
  const s = table($("#subs-table"), {
    rows: subsOf(view.rows),
    key: (r) => r.slug,
    name: (r) => r.name,
    noun: ["sub-service", "sub-services"],
    placeholder: "Filter by name, slug, parent, stack…",
    search: (r) => [r.name, r.slug, r.parent, r.stack ? r.stack.name : "", r.state.label].join(" "),
    sort: { key: "parent", dir: 1 },
    age: el("span", { class: "age" }),
    columns: [
      { key: "name", label: "Sub-service", sort: (r) => r.name.toLowerCase(),
        render: (r) => [el("a", { href: "/services/" + encodeURIComponent(r.slug), style: { fontWeight: "600", color: "var(--text)" } }, r.name), el("span", { class: "sub mono" }, r.slug)] },
      { key: "state", label: "State", sort: (r) => ["fail", "warn", "pending", "off", "ok"].indexOf(r.state.cls),
        render: (r) => el("span", { class: "pill " + r.state.cls, title: r.state.why || null }, r.state.label) },
      { key: "parent", label: "Part of", sort: (r) => (r.parent + " " + r.name).toLowerCase(),
        render: (r) => two(bySlug[r.parent] ? bySlug[r.parent].name : r.parent, r.parent, "/services/" + encodeURIComponent(r.parent) + "?tab=subs") },
      { key: "kind", label: "What", phone: false, sort: (r) => r.kind || "",
        render: (r) => r.stack ? two("Stack " + r.stack.name, r.containers ? r.containers + " container" + (r.containers === 1 ? "" : "s") : "") : two(r.kind === "stack" ? "Stack" : "Service", "") },
      { key: "check", label: "Last check", phone: false, render: (r) => two(r.check.text, r.check.at ? fmt.ago(r.check.at) : "") },
      { key: "manage", label: "", render: (r) => r.stack ? el("a", { class: "btn ghost small", href: r.stack.href }, "Manage stack →") : null },
    ],
    chips: [
      { key: "failed", label: "Failed", test: (r) => r.state.cls === "fail" },
      { key: "stacks", label: "Stacks", test: (r) => !!r.stack },
      { key: "off", label: "Off", test: (r) => r.state.cls === "off" },
    ],
    groupBy: [{ key: "parent", label: "Part of", of: (r) => r.parent }],
    menu: (r) => serviceActions(r, view, refresh),
    bulk: (rows) => {
      const slugs = rows.map((r) => r.slug);
      const run = (action) => () => bulk("services", action, slugs).then(() => { s.clearSelection(); refresh(); }, () => {});
      return [{ label: "Check", onClick: run("resync") }, { label: "Enable", onClick: run("enable") }, { label: "Disable", onClick: run("disable") }];
    },
    empty: { title: "No sub-services", text: "Adopt a Portainer stack, or give a service a parent, and it shows here." },
  });
  tabs($("#svc-list-tabs"));
  const counts = (rows) => { $("#n-tops").textContent = tops(rows).length; $("#n-subs").textContent = subsOf(rows).length; };
  counts(view.rows);

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
    render: (data) => { view = data; const rows = tops(data.rows); t.setRows(rows); s.setRows(subsOf(data.rows)); counts(data.rows); $("#svc-count").textContent = rows.length; },
    topics: ["health", "run", "registry"],
    wait: 1200,
    age,
  });
  function refresh() { r.refresh(); }

  $("#new-service").addEventListener("click", () => serviceDrawer({ view, onSaved: refresh }));
  if (new URLSearchParams(location.search).get("new") === "1") serviceDrawer({ view, onSaved: refresh });
})();
