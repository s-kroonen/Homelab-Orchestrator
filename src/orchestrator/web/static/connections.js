/* Settings → Connections (templates/connections.html), to the Connections wireframe: the
 * connections grouped by what they are for, each with whether it answers, Open, Edit and a menu.
 * Data: GET /api/connections (the file, at once); whether each answers comes from
 * GET /api/infra/overview right after (every check side by side). */
(function () {
  "use strict";
  const { $, el, clear, fill, api, drawer, confirm, menuButton } = window.UI;
  const toast = (m, k) => window.toast(m, k);
  const enc = encodeURIComponent;
  let V = JSON.parse($("#initial").textContent);
  let checks = {}; // id -> {state, detail, ms}
  let checking = false;

  const GROUPS = [
    { title: "Machines and backups", kinds: ["proxmox", "pbs", "power", "ssh"] },
    { title: "Containers", kinds: ["portainer", "haos"] },
    { title: "Proxy", kinds: ["proxy", "traefik_api"] },
    { title: "Alerts", note: "where notifications go", kinds: ["ntfy", "email", "webhook"] },
  ];
  const specOf = (kind) => V.kinds.find((k) => k.kind === kind);
  const STATE = {
    up: ["ok", "reachable"], down: ["fail", "unreachable"], off: ["off", "off"], incomplete: ["warn", "not filled in"],
    unchecked: ["off", "not checked"],
  };
  function stateOf(c) {
    if (!c.enabled) return ["off", "off"];
    if (!c.configured) return ["warn", "not filled in"];
    const r = checks[c.id];
    if (!r) return ["off", checking ? "checking…" : "not checked"];
    if (r.state === "up" && specOf(c.kind) && specOf(c.kind).channel) return ["ok", "on"];
    return STATE[r.state] || ["off", r.state];
  }
  function summary(c) {
    const spec = specOf(c.kind);
    const marker = spec && c.settings[spec.marker];
    const r = checks[c.id];
    return [marker ? String(marker) : "", r && r.detail ? r.detail : "", r && r.ms != null ? r.ms + " ms" : ""].filter(Boolean).join(" · ");
  }

  // --- the page ------------------------------------------------------------------------------
  function drawBanners() {
    const box = clear($("#banners"));
    $("#c-path").textContent = V.path;
    if (V.error) box.append(el("div", { class: "banner fail" }, el("strong", {}, "connections.yaml cannot be used"), ", so the .env values stay in force: " + V.error));
    const fromEnv = V.connections.filter((c) => c.source === "env");
    if (fromEnv.length) box.append(el("div", { class: "banner", style: { display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap" } },
      el("span", { class: "pill warn" }, ".env"),
      el("span", { style: { flex: "1" } }, fromEnv.map((c) => c.kind_title).join(", ") + (fromEnv.length === 1 ? " is" : " are") + " still configured in .env. Move " + (fromEnv.length === 1 ? "it" : "them") + " here so " + (fromEnv.length === 1 ? "it" : "they") + " can be edited and tested."),
      el("button", { type: "button", class: "secondary small", onclick: importEnv }, "Move into the file")));
    if (V.env_keys.length) box.append(el("div", { class: "banner info" }, "These .env keys are no longer used and can be deleted from .env: ", el("span", { class: "mono" }, V.env_keys.join(", "))));
  }
  function row(spec, c) {
    const [cls, label] = stateOf(c);
    const tags = [c.site ? c.site : "", spec.multi && c.default ? "default" : "", c.scope === "shared" ? "shared" : "", c.source === "env" ? "from .env" : ""].filter(Boolean);
    const primary = c.configured && checks[c.id] && checks[c.id].state === "down"
      ? el("button", { type: "button", class: "secondary small", onclick: () => test(c) }, "Test")
      : spec.channel && c.configured ? el("button", { type: "button", class: "secondary small", onclick: () => test(c) }, "Send a test")
      : el("button", { type: "button", class: "secondary small", onclick: () => connDrawer(spec, c) }, "Edit");
    return el("div", { class: "conn-row" },
      el("span", { class: "pill " + cls, style: { justifySelf: "start" } }, label),
      el("div", { style: { minWidth: 0 } },
        el("div", {}, el("strong", {}, c.name), " ", el("span", { class: "mono faint" }, c.id), tags.map((t) => [" ", el("span", { class: "tag" }, t)])),
        el("div", { class: "faint", style: { fontSize: "12.5px", overflowWrap: "anywhere" } }, summary(c) || spec.blurb)),
      el("a", { class: "btn ghost small", href: "/infra/connections/" + enc(c.id) }, "Open"),
      primary,
      menuButton(() => [
        { label: "Edit…", onClick: () => connDrawer(spec, c) },
        { label: spec.channel ? "Send a test" : "Test", onClick: () => test(c) },
        spec.multi && !c.default && c.enabled && c.source !== "env" ? { label: "Make it the default", onClick: () => makeDefault(c) } : null,
        c.source !== "env" ? "-" : null,
        c.source !== "env" ? { label: "Delete…", danger: true, onClick: () => remove(c) } : null,
      ], "Actions for " + c.name));
  }
  function emptyRow(spec) {
    return el("div", { class: "conn-row" },
      el("span", { class: "pill off", style: { justifySelf: "start" } }, "not set up"),
      el("div", { style: { minWidth: 0 } }, el("strong", {}, spec.title), el("div", { class: "faint", style: { fontSize: "12.5px" } }, spec.blurb)),
      el("span"), el("button", { type: "button", class: "secondary small", onclick: () => connDrawer(spec, null) }, "Set up"), el("span"));
  }
  function drawGroups() {
    const box = clear($("#groups"));
    for (const g of GROUPS) {
      const kinds = g.kinds.map(specOf).filter(Boolean);
      if (!kinds.length) continue;
      const multi = kinds.some((k) => k.multi);
      box.append(el("section", { class: "card flush" },
        el("div", { class: "card-head", style: { padding: "14px 18px 12px", margin: 0, borderBottom: "1px solid var(--line)" } }, el("h2", {}, g.title),
          el("span", { class: "faint", style: { fontSize: "12.5px" } }, g.note || (multi ? "several allowed" : "one of each")),
          el("span", { style: { flex: "1" } }),
          multi ? el("button", { type: "button", class: "ghost small", onclick: () => connDrawer(kinds.find((k) => k.multi), null) }, "+ Add another") : null),
        kinds.map((spec) => {
          const conns = V.connections.filter((c) => c.kind === spec.kind);
          return conns.length ? conns.map((c) => row(spec, c)) : emptyRow(spec);
        })));
    }
  }
  function render(view) { if (view) V = view; drawBanners(); drawGroups(); }

  // --- actions ------------------------------------------------------------------------------
  async function testAll() {
    checking = true; drawGroups();
    const btn = $("#test-all"); btn.disabled = true;
    try {
      const r = await api.get("/api/infra/overview", { suppressToast: true });
      checks = {}; for (const c of r.connections) checks[c.id] = c;
    } catch (e) { toast(e.message, "error"); }
    finally { checking = false; btn.disabled = false; drawGroups(); }
  }
  async function test(c) {
    try {
      const r = await api.post("/api/connections/" + enc(c.id) + "/test", {}, { suppressToast: true });
      checks[c.id] = { state: r.ok === true ? "up" : r.ok === false ? "down" : "unchecked", detail: r.detail, ms: r.ms };
      toast(c.name + ": " + r.detail + (r.ms != null ? " (" + r.ms + " ms)" : ""), r.ok === false ? "error" : "ok");
    } catch (e) { toast(e.message, "error"); }
    drawGroups();
  }
  async function makeDefault(c) {
    try {
      const r = await api.post("/api/connections/" + enc(c.id) + "/default", {}, { suppressToast: true });
      render(r);
      toast(r.pinned && r.pinned.length ? "Default changed; " + r.pinned.join(", ") + " stay on " + (r.default_was || "the old default") : "Default changed", "ok");
    } catch (e) { toast(e.message, "error"); }
  }
  async function remove(c) {
    if (!await confirm({ title: "Delete the connection " + c.name + "?", body: "Anything that uses it stops working until you point it elsewhere.", typeName: c.id, action: "Delete" })) return;
    try { render(await api.del("/api/connections/" + enc(c.id), { suppressToast: true })); toast("Deleted " + c.id, "ok"); }
    catch (e) {
      if (!/use\(s\) this connection/.test(e.message)) { toast(e.message, "error"); return; }
      if (!await confirm({ title: "It is still in use", body: e.message, action: "Delete anyway" })) return;
      try { render(await api.del("/api/connections/" + enc(c.id) + "?force=true")); toast("Deleted " + c.id, "ok"); } catch (_e) { /* toast */ }
    }
    loadControllers();
  }
  async function importEnv() {
    try {
      const r = await api.post("/api/connections/import-env", {}, { suppressToast: true });
      render(r);
      toast("Moved " + (r.moved.join(", ") || "nothing") + " into the file." + (r.delete_from_env.length ? " Delete from .env when convenient: " + r.delete_from_env.join(", ") : ""), "ok");
    } catch (e) { toast(e.message, "error"); }
  }

  // --- the drawer, built from the kind's fields ------------------------------------------------------
  function connDrawer(spec, c) {
    const editing = !!c;
    const values = { name: editing ? c.name : spec.title, id: "", enabled: editing ? c.enabled : true, default: false,
      site: editing ? c.site : V.site || "", scope: editing ? c.scope : "local", notes: editing ? c.notes : "" };
    const fields = [
      { key: "name", label: "Name", required: true },
      editing ? null : { key: "id", label: "Id", mono: true, placeholder: "made from the name", help: "How services and the CLI refer to it. Lower case, digits, - and _." },
      { key: "enabled", label: "Switched on", type: "checkbox" },
      spec.multi && !editing ? { key: "default", label: "Make it the default " + spec.title, type: "checkbox" } : null,
      { type: "section", label: spec.title },
    ];
    for (const f of spec.fields) {
      const key = "s_" + f.name;
      const isSet = editing && c.secrets_set && c.secrets_set[f.name];
      if (f.type === "bool") { values[key] = editing ? !!c.settings[f.name] : !!f.default; fields.push({ key, label: f.label, type: "checkbox", help: f.help || null }); continue; }
      if (f.type === "choice") { values[key] = editing ? c.settings[f.name] : f.default; fields.push({ key, label: f.label, type: "select", options: f.choices, help: f.help || null }); continue; }
      if (f.type === "secret") { values[key] = ""; fields.push({ key, label: f.label, type: "password", required: f.required && !isSet, placeholder: isSet ? "set: leave blank to keep" : "", help: f.help || null }); continue; }
      values[key] = editing ? (c.settings[f.name] ?? "") : (f.default ?? "");
      fields.push({ key, label: f.label, type: f.type === "int" || f.type === "float" ? "number" : "text", step: f.type === "float" ? "any" : null, required: f.required, mono: /url|host|path|topic/.test(f.name), help: f.help || null });
    }
    fields.push({ type: "section", label: "Where it lives" },
      { key: "site", label: "Site", placeholder: "home, offsite…" },
      { key: "scope", label: "Scope", type: "select", options: [{ value: "local", label: "Local: only this orchestrator" }, { value: "shared", label: "Shared: every orchestrator" }] },
      { key: "notes", label: "Notes" });
    const collect = (v) => {
      const settings = {}; const secrets = {};
      for (const f of spec.fields) {
        const val = v["s_" + f.name];
        if (f.type === "secret") secrets[f.name] = val || "";
        else settings[f.name] = f.type === "bool" ? !!val : val == null ? "" : val;
      }
      return { settings, secrets };
    };
    drawer({
      title: editing ? "Edit " + c.name : "Add " + spec.title, sub: spec.blurb, values,
      fields: fields.filter(Boolean),
      saveLabel: editing ? "Save" : "Add",
      note: "Applied at once, no restart.",
      testLabel: spec.channel ? "Send a test" : "Test",
      async onTest(v) {
        const r = await api.post("/api/connections/test", Object.assign({ kind: spec.kind, existing_id: editing ? c.id : "" }, collect(v)), { suppressToast: true });
        const text = r.detail + (r.ms != null ? " (" + r.ms + " ms)" : "");
        if (r.ok === false) throw new Error(text);
        return text;
      },
      onDelete: editing && c.source !== "env" ? async () => { await remove(c); return false; } : null,
      async onSave(v) {
        const common = Object.assign({ name: v.name, enabled: !!v.enabled, site: v.site || "", scope: v.scope, notes: v.notes || "" }, collect(v));
        const view = editing
          ? await api.put("/api/connections/" + enc(c.id), common, { suppressToast: true })
          : await api.post("/api/connections", Object.assign(common, { kind: spec.kind, id: (v.id || "").trim(), default: !!v.default }), { suppressToast: true });
        render(view);
        toast("Saved and applied", "ok");
        loadControllers();
        const saved = view.connections.find((x) => x.name === v.name && x.kind === spec.kind);
        if (saved) test(saved);
      },
    });
  }
  function pickKind() {
    const box = el("div", { style: { display: "flex", flexDirection: "column", gap: "6px" } });
    const d = drawer({ title: "Add a connection", fields: [{ type: "custom", key: "_k", render: () => box }], saveLabel: "Close", onSave: () => {} });
    for (const g of GROUPS) {
      box.append(el("h3", {}, g.title));
      for (const spec of g.kinds.map(specOf).filter(Boolean)) {
        const has = V.connections.some((c) => c.kind === spec.kind);
        box.append(el("button", { type: "button", class: "secondary", disabled: has && !spec.multi, style: { justifyContent: "flex-start", height: "auto", padding: "8px 12px", textAlign: "left" },
          onclick: () => { d.close(true); connDrawer(spec, null); } },
        el("span", {}, el("strong", {}, spec.title), el("div", { class: "faint", style: { fontWeight: "400", fontSize: "12px" } }, has && !spec.multi ? "already set up: edit it in the list" : spec.blurb))));
      }
    }
  }

  // --- controllers, sharing ---------------------------------------------------------------------
  async function loadControllers() {
    const box = $("#controllers");
    let rows = [];
    try { rows = await api.get("/api/connections/controllers", { suppressToast: true }); } catch (_e) { return; }
    if (!rows.length) { fill(box, el("p", { class: "muted", style: { margin: "0 0 8px" } }, "No service has a controller yet.")); return; }
    fill(box, rows.map((r) => el("div", { class: "event-row", style: { gridTemplateColumns: "minmax(0,1fr) auto auto" } },
      el("a", { href: "/services/" + enc(r.slug) }, r.name || r.slug),
      el("span", { class: "mono faint" }, r.type + (r.connection ? " → " + r.connection : "")),
      r.usable ? el("span", { class: "pill ok" }, "ok") : el("span", { class: "pill fail", title: "this connection is missing or switched off" }, "missing"))));
  }
  $("#export").addEventListener("click", async () => {
    const text = el("textarea", { rows: 14, readonly: true, class: "mono", "aria-label": "Export" });
    const secrets = el("input", { type: "checkbox" });
    const load = async () => { try { const r = await fetch("/api/connections/export?with_secrets=" + secrets.checked, { credentials: "same-origin" }); text.value = await r.text(); } catch (e) { toast(e.message, "error"); } };
    secrets.addEventListener("change", load);
    drawer({ title: "Export shared connections", fields: [{ type: "custom", key: "_x", render: () => el("div", {}, el("label", { class: "inline", style: { marginBottom: "10px" } }, secrets, " include secrets"), text) }],
      saveLabel: "Copy", onSave: async () => { try { await navigator.clipboard.writeText(text.value); toast("Copied", "ok"); } catch (_e) { text.select(); toast("Select it and copy it by hand", "error"); throw new Error("not copied"); } } });
    load();
  });
  $("#import").addEventListener("click", () => drawer({
    title: "Import connections", values: { body: "" }, saveLabel: "Import",
    intro: "Paste an export from another orchestrator. What arrives is switched off: add its secrets and turn it on.",
    fields: [{ key: "body", label: "Export", type: "textarea", rows: 12, required: true }],
    async onSave(v) { const r = await api.post("/api/connections/import", { body: v.body }, { suppressToast: true }); render(r); toast("Added " + (r.added.join(", ") || "nothing") + "; already here: " + (r.skipped.join(", ") || "none"), "ok"); },
  }));

  $("#test-all").addEventListener("click", testAll);
  $("#add-conn").addEventListener("click", pickKind);
  render();
  loadControllers();
  testAll();
  if (window.OrchLive) window.OrchLive.on(["audit"], (d) => { if (/^connection/.test(d.action || "")) api.get("/api/connections", { suppressToast: true }).then(render, () => {}); }, { wait: 1000 });
})();
