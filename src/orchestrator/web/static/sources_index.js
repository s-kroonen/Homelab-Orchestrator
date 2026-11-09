/* Sources → All sources (templates/sources_index.html), to the Sources and SourceAdd wireframes:
 * every source with its status, what it has and what uses it; Test all; Add source (pick a kind,
 * then its fields); ⋯ per source; export and import of shared connections.
 * Data: embedded at first paint (last known status); GET /api/sources after a change;
 * POST /api/sources/check for Test all (and once on open when nothing was checked yet). */
(function () {
  "use strict";
  const { $, el, clear, fill, api, fmt, table, drawer, menuButton } = window.UI;
  const toast = (m, k) => window.toast(m, k);
  const enc = encodeURIComponent;
  const V = JSON.parse($("#initial").textContent);
  let rows = V.sources;

  const STATE = { ok: "ok", warn: "warn", fail: "fail", off: "off", unknown: "off" };
  const pill = (st) => el("span", { class: "pill " + (STATE[st.state] || "off"), title: st.detail || "" }, st.label || st.state);
  const kindOf = (k) => V.kinds.find((x) => x.kind === k);

  function connOf(r) { return api.get("/api/connections", { suppressToast: true }).then((v) => v.connections.find((c) => c.id === r.id)); }

  async function editSource(r) {
    const conn = await connOf(r);
    if (!conn) { toast("Not found", "error"); return; }
    window.ConnDrawer.open({ kind: Object.assign({}, kindOf(r.kind), { blurb: kindOf(r.kind).blurb }), conn, onSaved: reload, onRemoved: reload });
  }
  async function testOne(r) {
    try {
      const row = await api.post("/api/sources/" + enc(r.id) + "/test", {}, { suppressToast: true });
      rows = rows.map((x) => (x.id === row.id ? row : x)); t.setRows(rows);
      toast(r.name + ": " + (row.status.label || row.status.state) + (row.status.detail ? " · " + row.status.detail : ""), row.status.state === "fail" ? "error" : "ok");
    } catch (e) { toast(e.message, "error"); }
  }
  async function makeDefault(r) {
    try { await api.post("/api/connections/" + enc(r.id) + "/default", {}, { suppressToast: true }); toast(r.name + " is the default " + r.kind_title, "ok"); reload(); }
    catch (e) { toast(e.message, "error"); }
  }
  async function setEnabled(r, on) {
    const conn = await connOf(r);
    if (!conn) return;
    try {
      await api.put("/api/connections/" + enc(r.id), { name: conn.name, enabled: on, site: conn.site, scope: conn.scope, notes: conn.notes, settings: conn.settings, secrets: {} }, { suppressToast: true });
      toast(r.name + (on ? " switched on" : " switched off"), "ok"); reload();
    } catch (e) { toast(e.message, "error"); }
  }

  const chips = [{ key: "problems", label: "Problems", test: (r) => ["fail", "warn"].includes(r.status.state) }];
  for (const g of Array.from(new Set(V.kinds.map((k) => k.group)))) chips.push({ key: "g-" + g.toLowerCase().replace(/\W+/g, "-"), label: g, test: (r) => r.group === g });

  const t = table($("#src-table"), {
    rows, key: (r) => r.id, noun: ["source", "sources"], placeholder: "Filter sources",
    search: (r) => [r.name, r.id, r.kind_title, r.address, r.status.has].join(" "),
    chips, name: (r) => r.name, href: (r) => r.href,
    columns: [
      { key: "name", label: "Source", sort: (r) => r.name.toLowerCase(), render: (r) => [
        el("a", { href: r.href }, el("strong", {}, r.name)), r.default && rows.filter((x) => x.kind === r.kind).length > 1 ? [" ", el("span", { class: "tag" }, "default")] : null,
        r.from_env ? [" ", el("span", { class: "tag", title: "Still configured in .env: Move into the file" }, ".env")] : null,
        el("div", { class: "mono faint", style: { fontSize: "12.5px" } }, r.address || "—")] },
      { key: "kind", label: "Kind", sort: (r) => r.kind_title, render: (r) => el("span", { class: "kindtag" }, r.kind_title) },
      { key: "status", label: "Status", sort: (r) => r.status.state, render: (r) => [pill(r.status), r.status.ms != null ? [" ", el("span", { class: "faint" }, r.status.ms + " ms")] : null] },
      { key: "has", label: "What it has", sort: (r) => r.status.has || "", render: (r) => r.status.has || (r.status.detail ? el("span", { class: "faint" }, r.status.detail) : el("span", { class: "faint" }, "—")) },
      { key: "used", label: "Used by", sort: (r) => r.used_by.length, render: (r) => r.used_by.length ? el("span", { title: r.used_by.join(", ") }, r.used_by.length + (r.used_by.length === 1 ? " service" : " services")) : el("span", { class: "faint" }, "—") },
      { key: "checked", label: "Checked", sort: (r) => r.status.checked_at || 0, render: (r) => r.status.checked_at ? el("span", { class: "faint" }, fmt.ago(new Date(r.status.checked_at * 1000).toISOString())) : el("span", { class: "faint" }, "never") },
    ],
    menu: (r) => [
      { label: "Open", href: r.href },
      { label: "Test", onClick: () => testOne(r) },
      { label: "Edit settings…", onClick: () => editSource(r) },
      kindOf(r.kind) && kindOf(r.kind).multi && !r.default && r.enabled && !r.from_env ? { label: "Make it the default", onClick: () => makeDefault(r) } : null,
      r.from_env ? null : r.enabled ? { label: "Switch off", onClick: () => setEnabled(r, false) } : { label: "Switch on", onClick: () => setEnabled(r, true) },
      r.from_env ? null : "-",
      r.from_env ? null : { label: "Remove…", danger: true, onClick: async () => { const conn = await connOf(r); if (conn) window.ConnDrawer.remove(conn, reload); } },
    ],
    empty: { title: "No sources yet", text: "Add a Proxmox cluster, a Portainer, a proxy, a backup server or a machine.", action: el("button", { type: "button", onclick: () => pickKind() }, "+ Add source") },
  });

  async function reload() {
    try { rows = await api.get("/api/sources", { suppressToast: true }); t.setRows(rows); $("#src-count").textContent = rows.length; banners(); }
    catch (e) { toast(e.message, "error"); }
  }

  function banners() {
    const box = clear($("#src-banners"));
    const fromEnv = rows.filter((r) => r.from_env);
    if (fromEnv.length) box.append(el("div", { class: "banner", style: { display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap", marginBottom: "14px" } },
      el("span", { class: "pill warn" }, ".env"),
      el("span", { style: { flex: "1" } }, fromEnv.map((r) => r.name).join(", ") + (fromEnv.length === 1 ? " is" : " are") + " still configured in .env. Move " + (fromEnv.length === 1 ? "it" : "them") + " into connections.yaml so " + (fromEnv.length === 1 ? "it" : "they") + " can be edited here (and stored encrypted)."),
      el("button", { type: "button", class: "secondary small", onclick: importEnv }, "Move into the file")));
  }
  async function importEnv() {
    try {
      const r = await api.post("/api/connections/import-env", {}, { suppressToast: true });
      toast("Moved " + (r.moved.join(", ") || "nothing") + " into the file." + (r.delete_from_env.length ? " Delete from .env when convenient: " + r.delete_from_env.join(", ") : ""), "ok");
      reload();
    } catch (e) { toast(e.message, "error"); }
  }

  async function testAll() {
    const btn = $("#test-all"); btn.disabled = true; btn.textContent = "Testing…";
    try { rows = await api.post("/api/sources/check", {}, { suppressToast: true }); t.setRows(rows); }
    catch (e) { toast(e.message, "error"); }
    finally { btn.disabled = false; btn.textContent = "Test all"; }
  }

  // --- Add source: pick what it is, then its fields (SourceAdd wireframe) ---------------------------
  const FAMILIES = [["machines", "Machines"], ["apps", "Apps and containers"], ["network", "Network, backups, power"]];
  function pickKind() {
    const box = el("div", { style: { display: "flex", flexDirection: "column", gap: "8px" } });
    const d = drawer({ title: "Add a source", sub: "Pick what it is. Its settings come next, with a Test before Save.", fields: [{ type: "custom", key: "_k", render: () => box }], saveLabel: "Close", onSave: () => {} });
    for (const [fam, label] of FAMILIES) {
      const kinds = V.kinds.filter((k) => k.family === fam);
      if (!kinds.length) continue;
      box.append(el("div", { class: "pick-family" }, label));
      const grid = el("div", { class: "pick-kind" }); box.append(grid);
      for (const k of kinds) {
        const has = rows.some((r) => r.kind === k.kind);
        grid.append(el("button", { type: "button", disabled: has && !k.multi,
          onclick: () => { d.close(true); window.ConnDrawer.open({ kind: k, onSaved: (saved) => { reload().then(() => { if (saved) location.href = "/sources/" + enc(saved.id); }); } }); } },
        el("strong", {}, k.title), el("span", { class: "faint" }, has && !k.multi ? "already set up" : k.blurb)));
      }
    }
  }

  // --- sharing between orchestrators ------------------------------------------------------------------
  fill($("#more-actions"), menuButton(() => [
    { label: "Export shared connections…", onClick: exportShared },
    { label: "Import connections…", onClick: importShared },
  ], "More actions"));
  function exportShared() {
    const text = el("textarea", { rows: 14, readonly: true, class: "mono", "aria-label": "Export" });
    const secrets = el("input", { type: "checkbox" });
    const load = async () => { try { const r = await fetch("/api/connections/export?with_secrets=" + secrets.checked, { credentials: "same-origin" }); text.value = await r.text(); } catch (e) { toast(e.message, "error"); } };
    secrets.addEventListener("change", load);
    drawer({ title: "Export shared connections", fields: [{ type: "custom", key: "_x", render: () => el("div", {}, el("label", { class: "inline", style: { marginBottom: "10px" } }, secrets, " include secrets (in plain text: the other orchestrator has its own key)"), text) }],
      saveLabel: "Copy", onSave: async () => { try { await navigator.clipboard.writeText(text.value); toast("Copied", "ok"); } catch (_e) { text.select(); toast("Select it and copy it by hand", "error"); throw new Error("not copied"); } } });
    load();
  }
  function importShared() {
    drawer({
      title: "Import connections", values: { body: "" }, saveLabel: "Import",
      intro: "Paste an export from another orchestrator. What arrives is switched off: add its secrets and turn it on.",
      fields: [{ key: "body", label: "Export", type: "textarea", rows: 12, required: true }],
      async onSave(v) { const r = await api.post("/api/connections/import", { body: v.body }, { suppressToast: true }); toast("Added " + (r.added.join(", ") || "nothing") + "; already here: " + (r.skipped.join(", ") || "none"), "ok"); reload(); },
    });
  }

  $("#test-all").addEventListener("click", testAll);
  $("#add-source").addEventListener("click", pickKind);
  if (new URLSearchParams(location.search).get("add")) { history.replaceState(history.state, "", "/sources"); pickKind(); }
  banners();
  if (rows.some((r) => r.enabled && !r.status.checked_at)) testAll();
  if (window.OrchLive) window.OrchLive.on(["audit"], (d) => { if (/^connection/.test(d.action || "")) reload(); }, { wait: 1000 });
})();
