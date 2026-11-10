/* One source (templates/source.html), to the SourceProxmox, SourceSettings and SourceRaw
 * wireframes. The header (status, address, what uses it, Test, ⋯) and four kinds of tab:
 *   overview      tiles, what needs attention, this source's facts      GET /api/sources/{id}/overview
 *   resource:key  one list (generic table, or a kind's own module)      GET /api/sources/{id}/resources/{key}
 *   settings      short cards, each saved on its own                    PUT /api/connections/{id}
 *   raw           the API's own data, credentials redacted              GET /api/sources/{id}/raw/{key}
 * A kind with its own table registers window.SourceViews["proxmox.guests"] = (panel, ctx) => {...}
 * before this file runs; anything not registered gets the generic table. */
(function () {
  "use strict";
  const { $, $$, el, clear, fill, api, fmt, table, tabs, region, menu, menuButton, confirm } = window.UI;
  const toast = (m, k) => window.toast(m, k);
  const enc = encodeURIComponent;
  let S = JSON.parse($("#initial").textContent);
  const ID = S.id;
  const base = "/api/sources/" + enc(ID);

  const STATE = { ok: "ok", warn: "warn", fail: "fail", off: "off", unknown: "off" };
  const statePill = (st) => el("span", { class: "pill " + (STATE[st.state] || "off"), title: st.detail || "" }, st.label || st.state);
  const toneOf = (v) => {
    const s = String(v == null ? "" : v).toLowerCase();
    if (/^(ok|online|running|up|healthy|reachable|active|yes|true)$/.test(s)) return "ok";
    if (/(down|fail|error|unreachable|dead|exited)/.test(s)) return "fail";
    if (/(warn|degraded|partial|paused|full)/.test(s)) return "warn";
    return "off";
  };

  // --- header ----------------------------------------------------------------------------------
  function usedBy() {
    const n = S.used_by.length;
    if (!n) return el("span", { class: "faint" }, "used by nothing yet");
    const btn = el("a", { href: "#", "aria-haspopup": "menu" }, n + (n === 1 ? " service" : " services"));
    btn.addEventListener("click", (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      menu(btn, S.used_by.map((name) => ({ label: name, href: "/services/" + enc(name) })));
    });
    return ["used by ", btn];
  }
  function renderHead() {
    $("#src-name").textContent = S.name;
    fill($("#src-pill"), S.enabled ? statePill(S.status) : el("span", { class: "pill off" }, "switched off"));
    const st = S.status; const bits = [];
    if (S.address) bits.push(el("span", { class: "mono" }, S.address));
    if (st.version) bits.push(st.version);
    if (st.ms != null) bits.push(st.ms + " ms");
    if (st.has) bits.push(st.has);
    bits.push(usedBy());
    const sub = $("#src-sub"); clear(sub);
    bits.forEach((b, i) => { if (i) sub.append(" · "); sub.append(...[].concat(b)); });
    if (st.state === "fail" && st.detail) sub.append(el("div", { class: "field-error", style: { marginTop: "4px" } }, st.detail));
    fill($("#src-more"), menuButton(() => moreItems(), "More for " + S.name));
    renderBanners();
  }
  function moreItems() {
    const items = [{ label: "Settings", onClick: () => tabCtl.show("settings") }];
    if (S.multi && !S.default) items.push({ label: "Make it the default " + S.kind_title, onClick: makeDefault });
    if (!S.from_env) {
      items.push({ label: S.enabled ? "Switch off" : "Switch on", onClick: () => setEnabled(!S.enabled) });
      items.push("-", { label: "Remove…", danger: true, onClick: remove });
    }
    return items;
  }
  function renderBanners() {
    const box = $("#src-banners"); clear(box);
    if (S.from_env) {
      box.append(el("div", { class: "banner warn" },
        el("span", {}, "This source is configured in ", el("code", {}, ".env"), ". Move it into the connections file to edit it here."), " ",
        el("button", { type: "button", class: "small", onclick: importEnv }, "Move into the file")));
    } else if (!S.enabled) {
      box.append(el("div", { class: "banner" }, el("span", {}, "Switched off: nothing reads from or acts on it. Its settings are kept."), " ",
        el("button", { type: "button", class: "small", onclick: () => setEnabled(true) }, "Switch on")));
    }
  }

  async function reload() {
    try { S = await api.get(base, { suppressToast: true }); renderHead(); renderSettings(); }
    catch (e) { if (/no source called/.test(e.message)) location.href = "/sources"; }
  }
  async function testNow(quiet) {
    const btn = $("#src-test"); btn.disabled = true; btn.textContent = "Testing…";
    try {
      const row = await api.post(base + "/test", {}, { suppressToast: true });
      Object.assign(S, row); renderHead(); renderSettings();
      if (quiet === true) return;
      toast(S.name + ": " + (row.status.label || row.status.state) + (row.status.detail ? " · " + row.status.detail : ""), row.status.state === "fail" ? "error" : "ok");
    } catch (e) { toast(e.message, "error"); }
    finally { btn.disabled = false; btn.textContent = "Test"; }
  }
  async function makeDefault() {
    try { await api.post("/api/connections/" + enc(ID) + "/default", {}, { suppressToast: true }); toast(S.name + " is the default " + S.kind_title, "ok"); reload(); }
    catch (e) { toast(e.message, "error"); }
  }
  async function setEnabled(on) {
    try { await api.put("/api/connections/" + enc(ID), { enabled: on }, { suppressToast: true }); toast(S.name + (on ? " switched on" : " switched off"), "ok"); location.reload(); }
    catch (e) { toast(e.message, "error"); }
  }
  async function importEnv() {
    try { await api.post("/api/connections/import-env", {}, { suppressToast: true }); toast("Moved into the file", "ok"); reload(); }
    catch (e) { toast(e.message, "error"); }
  }
  function remove() { window.ConnDrawer.remove(S.connection, () => { location.href = "/sources"; }); }
  $("#src-test").addEventListener("click", testNow);

  // --- tabs ------------------------------------------------------------------------------------
  const panels = new Map($$("[role=tabpanel][data-tab]").map((p) => [p.dataset.tab, p]));
  const started = new Set();
  const setCount = (key, n) => { const c = $('[data-count="' + key + '"]'); if (c) c.textContent = n == null ? "" : String(n); };
  function start(key) {
    if (started.has(key)) return;
    started.add(key);
    const p = panels.get(key); const view = p.dataset.view;
    if (view === "overview") overview(p);
    else if (view === "settings") renderSettings();
    else if (view === "raw") raw(p);
    else if (view.startsWith("resource:")) resource(p, view.slice(9));
  }
  let tabCtl = null;

  function off(p) {
    fill(p, el("div", { class: "card" }, el("div", { class: "dt-empty" }, el("strong", {}, "Switched off"),
      el("div", {}, "Switch it on to see what it has."))));
  }

  // --- overview --------------------------------------------------------------------------------
  function tile(t) {
    return el("div", { class: "stat-tile" },
      el("div", { class: "stat-label" }, t.label),
      el("div", { class: "stat-value", style: t.tone ? { color: "var(--" + t.tone + ")" } : null }, String(t.value),
        t.of != null ? el("span", { class: "faint", style: { fontSize: "14px", fontWeight: "500" } }, " / " + t.of + (t.sub ? " " + t.sub : "")) : null),
      t.of == null && t.sub ? el("div", { class: "stat-sub" }, t.sub) : null);
  }
  function overview(p) {
    if (!S.enabled) { off(p); return; }
    const tilesBox = el("div", { class: "stat-row stat-row-5" });
    const attention = el("div");
    const age = el("span", { class: "faint", style: { fontSize: "12.5px" } });
    const facts = el("dl", { class: "facts" });
    const recentBox = el("div");
    const sectionsBox = el("div", { class: "lane", style: { gap: "18px" } });
    // a link to another tab of this page opens the tab instead of loading the page again
    const tabLink = (href, label) => el("a", { href, onclick: (ev) => { const m = /^\?tab=(\w+)$/.exec(href); if (m) { ev.preventDefault(); tabCtl.show(m[1]); } } }, label);
    fill(p,
      tilesBox,
      el("div", { class: "grid-2" },
        el("div", { class: "col-main stack" },
          sectionsBox,
          el("div", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "Needs attention"), el("span", { class: "n faint", id: "att-n" }), el("div", { style: { flex: "1" } }), age), attention)),
        el("div", { class: "col-side", style: { display: "flex", flexDirection: "column", gap: "18px" } },
          el("div", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "This source"), el("div", { style: { flex: "1" } }),
            el("a", { href: "?tab=settings", onclick: (ev) => { ev.preventDefault(); tabCtl.show("settings"); } }, "Settings")), facts),
          el("div", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "Recent"), el("div", { style: { flex: "1" } }),
            el("a", { href: "/activity" }, "Activity")), recentBox))));
    region(recentBox, {
      load: () => api.get(base + "/recent", { suppressToast: true }), every: 60000, topics: ["audit"],
      render: (rows) => fill(recentBox, rows.length ? el("div", { class: "lane", style: { gap: "6px", fontSize: "13px" } }, rows.map((r) =>
        el("div", {}, el("span", { class: "faint", title: fmt.time(r.at) }, fmt.ago(r.at)), " ", el("strong", {}, r.action.replace(/[._]/g, " ")), " ",
          el("span", { class: "mono faint" }, r.target.replace(/^\w+:/, "")), r.result !== "ok" ? [" ", el("span", { class: "pill fail" }, r.result)] : null, " · " + r.actor)))
        : el("p", { class: "faint", style: { margin: "0" } }, "Nothing yet.")),
    });
    region(attention, {
      age, every: 60000,
      load: () => api.get(base + "/overview", { suppressToast: true }),
      render(ov) {
        fill(tilesBox, (ov.tiles || []).map(tile)); tilesBox.hidden = !(ov.tiles || []).length;
        const items = ov.attention || [];
        $("#att-n").textContent = items.length ? String(items.length) : "";
        fill(attention, items.length ? el("div", { class: "lane" }, items.map((a) => el("div", { style: { display: "flex", gap: "10px", alignItems: "baseline", flexWrap: "wrap" } },
          el("span", { class: "pill " + (a.tone || "warn") }, a.label || ""), el("span", { style: { flex: "1", minWidth: "0" } }, a.text),
          a.href ? tabLink(a.href, a.action || "Open") : null))) : el("p", { class: "faint", style: { margin: "0" } }, "Nothing. All is well."));
        fill(sectionsBox, (ov.sections || []).map((sec) => el("div", { class: "card flush" },
          el("div", { class: "card-head" }, el("h2", {}, sec.title), el("div", { style: { flex: "1" } }), sec.href ? tabLink(sec.href, "All") : null),
          el("div", { class: "dt-scroll" }, el("table", { class: "dt" },
            el("thead", {}, el("tr", {}, sec.columns.map((c) => el("th", { scope: "col", class: ["num", "pct", "bytes"].includes(c.kind) ? "num" : null }, c.label)))),
            el("tbody", {}, sec.rows.map((r) => el("tr", {}, sec.columns.map((c, i) => el("td", { class: i === 0 ? "first" : null, "data-label": i ? c.label : null }, i === 0 ? el("strong", {}, String(r[c.key])) : cell(c, r) || "—"))))))))));
        sectionsBox.hidden = !(ov.sections || []).length;
        fill(facts, (ov.facts || []).map(([k, v]) => [el("dt", {}, k), el("dd", { class: /^[\w.-]+:\/\/|^\d+\.\d+\.\d+\.\d+|^[\w.-]+\.(lan|local|com|net|org|io)\b|^[\w.-]+@[\w.!-]+$/.test(v) ? "mono" : null }, v)]));
      },
      onError(e) { fill(facts, el("dt", {}, "Error"), el("dd", { class: "field-error" }, e.message)); },
    });
  }

  // --- a resource tab: the generic table ---------------------------------------------------------
  function cell(c, r) {
    const v = r[c.key];
    if (v == null || v === "") return null;
    switch (c.kind) {
      case "pill": return el("span", { class: "pill " + (r[c.key + "_tone"] || toneOf(v)) }, String(v));
      case "mono": return el("span", { class: "mono" }, String(v));
      case "num": return String(v);
      case "pct": return el("span", { style: v >= 90 ? { color: "var(--fail)" } : v >= 75 ? { color: "var(--warn)" } : null }, v + "%");
      case "bytes": return fmt.bytes(v);
      case "ago": return el("span", { title: fmt.time(v) }, fmt.ago(v));
      case "time": return fmt.time(v);
      case "link": return r[c.key + "_href"] ? el("a", { href: r[c.key + "_href"] }, String(v)) : String(v);
      default: return String(v);
    }
  }
  function resource(p, key) {
    const meta = S.resources[key];
    if (!S.enabled) { off(p); return; }
    const custom = window.SourceViews && window.SourceViews[meta.view];
    const ctx = { source: S, key, meta, base, load: () => api.get(base + "/resources/" + enc(key), { suppressToast: true }).then((d) => { setCount(key, d.rows.length); return d.rows; }), setCount: (n) => setCount(key, n) };
    if (custom) { custom(p, ctx); return; }
    const box = el("div"); fill(p, box);
    const age = el("span", { class: "faint" });
    let t = null;
    region(box, {
      age, every: 60000, load: ctx.load,
      render(rows) {
        if (t) { t.setRows(rows); return; }
        const cols = meta.columns.map((c, i) => ({
          key: c.key, label: c.label, sort: (r) => r[c.key], cls: c.kind === "num" || c.kind === "pct" || c.kind === "bytes" ? "num" : null,
          render: i === 0 ? (r) => [r.href ? el("a", { href: r.href }, el("strong", {}, String(r[c.key] ?? r.id))) : el("strong", {}, String(r[c.key] ?? r.id)),
            (r.links || []).length ? el("div", { class: "faint", style: { fontSize: "12.5px" } }, r.links.map((l, j) => [j ? " · " : "", el("a", { href: l.href }, l.label)])) : null]
            : (r) => cell(c, r),
        }));
        t = table(box, {
          rows, key: (r) => r.id, noun: [meta.label.toLowerCase().replace(/s$/, ""), meta.label.toLowerCase()], placeholder: "Filter " + meta.label.toLowerCase(),
          urlState: false, columns: cols, age,
          chips: meta.chips.map((c) => ({ key: c.key, label: c.label, test: (r) => (r.chips || []).includes(c.key) })),
          name: (r) => String(r.name || r.id), href: (r) => r.href || null,
          empty: { title: "No " + meta.label.toLowerCase(), text: S.name + " has none right now." },
        });
      },
    });
  }

  // --- settings: short cards, each saved on its own ---------------------------------------------------
  const settingsPanel = panels.get("settings");
  function cardFor(title, fields, opts) {
    const o = opts || {};
    const readonly = !!S.from_env;
    const conn = S.connection;
    const start = {}; const now = {};
    for (const f of fields) start[f.key] = f.get(conn);
    Object.assign(now, start);
    const tag = el("span", { class: "pill pending", hidden: true }, "changed");
    const undo = el("button", { type: "button", class: "secondary small", hidden: true }, "Undo");
    const save = el("button", { type: "button", class: "small", hidden: true }, "Save");
    const err = el("div", { class: "banner fail form-error", role: "alert", hidden: true });
    const body = el("div", { class: o.cols ? "form-cols" : null, style: o.cols ? { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: "0 16px" } : null });
    const card = el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, title), tag, el("div", { style: { flex: "1" } }), undo, save), err, body);
    const dirty = () => fields.some((f) => JSON.stringify(now[f.key]) !== JSON.stringify(start[f.key]));
    const refresh = () => { const d = dirty(); tag.hidden = undo.hidden = save.hidden = !d; card.style.borderColor = d ? "var(--accent)" : ""; };
    function draw() {
      clear(body);
      for (const f of fields) {
        const id = "set-" + f.key.replace(/\W/g, "_");
        const help = f.help ? el("div", { class: "help" }, f.help) : null;
        let input;
        if (f.type === "checkbox") {
          input = el("input", { type: "checkbox", class: "switch", id, disabled: readonly || f.readonly });
          input.checked = !!now[f.key];
          input.addEventListener("change", () => { now[f.key] = input.checked; refresh(); });
          body.append(el("div", { class: "field check", style: { gridColumn: "1 / -1" } }, input, el("label", { for: id }, f.label), help));
          continue;
        }
        if (f.type === "list") {
          // a comma-separated list, shown as chips with an Add box
          const items = () => String(now[f.key] || "").split(",").map((x) => x.trim()).filter(Boolean);
          const set = (list) => { now[f.key] = list.join(","); refresh(); draw(); };
          const add = el("input", { id, type: "text", placeholder: "+ Add", readonly, style: { width: "9rem" }, "aria-label": "Add to " + f.label });
          add.addEventListener("keydown", (ev) => {
            if (ev.key !== "Enter" || !add.value.trim()) return;
            ev.preventDefault();
            set(items().concat(add.value.split(",").map((x) => x.trim()).filter(Boolean)));
            setTimeout(() => { const again = body.querySelector("#" + id); if (again) again.focus(); }, 0);
          });
          body.append(el("div", { class: "field", style: { gridColumn: "1 / -1" } }, el("label", { for: id }, f.label),
            el("div", { style: { display: "flex", gap: "6px", flexWrap: "wrap", alignItems: "center" } },
              items().map((x, i) => el("span", { class: "chip" }, x, readonly ? null : el("button", { type: "button", class: "ghost small", "aria-label": "Remove " + x, style: { padding: "0 0 0 6px" },
                onclick: () => set(items().filter((_y, j) => j !== i)) }, "×"))), readonly ? null : add), help));
          continue;
        }
        if (f.type === "select") {
          input = el("select", { id, disabled: readonly }, f.options.map((x) => typeof x === "object" ? el("option", { value: x.value }, x.label) : el("option", { value: x }, x)));
          input.value = now[f.key] == null ? "" : String(now[f.key]);
          input.addEventListener("change", () => { now[f.key] = input.value; refresh(); });
        } else {
          input = el("input", { id, type: f.type || "text", readonly, autocomplete: f.type === "password" ? "new-password" : "off", spellcheck: "false",
            class: f.mono ? "mono" : null, placeholder: f.placeholder || null, step: f.step || null });
          input.value = now[f.key] == null ? "" : String(now[f.key]);
          input.addEventListener("input", () => { now[f.key] = f.type === "number" ? (input.value === "" ? null : Number(input.value)) : input.value; refresh(); });
        }
        body.append(el("div", { class: "field" }, el("label", { for: id }, f.label, f.required ? el("span", { class: "faint" }, " *") : null), input, help));
      }
    }
    undo.addEventListener("click", () => { Object.assign(now, start); err.hidden = true; draw(); refresh(); });
    save.addEventListener("click", async () => {
      const body2 = {}; const settings = {}; const secrets = {};
      for (const f of fields) {
        if (JSON.stringify(now[f.key]) === JSON.stringify(start[f.key])) continue;
        if (f.secret) secrets[f.name] = now[f.key] || "";
        else if (f.setting) settings[f.name] = now[f.key];
        else if (!f.local) body2[f.key] = now[f.key];
      }
      if (Object.keys(settings).length) body2.settings = settings;
      if (Object.keys(secrets).length) body2.secrets = secrets;
      save.disabled = true; err.hidden = true;
      try {
        if (o.onSave) await o.onSave(now, start);
        if (Object.keys(body2).length) await api.put("/api/connections/" + enc(ID), body2, { suppressToast: true });
        toast("Saved and applied", "ok");
        await reload();
      } catch (e) { err.textContent = e.message; err.hidden = false; }
      finally { save.disabled = false; }
    });
    draw(); refresh();
    return card;
  }
  function settingField(f) {
    const base2 = { key: "s_" + f.name, name: f.name, label: f.label, help: f.help || null, required: f.required, setting: true, card: f.card || "connection" };
    if (f.type === "secret") {
      return Object.assign(base2, { setting: false, secret: true, type: "password", get: () => "",
        placeholder: S.connection.secrets_set[f.name] ? "set · leave blank to keep" : "not set",
        help: (f.help ? f.help + " " : "") + "Stored encrypted; never shown again." });
    }
    const get = (c) => (c.settings[f.name] ?? f.default ?? "");
    if (f.type === "list") return Object.assign(base2, { type: "list", get: (c) => String(c.settings[f.name] ?? "") });
    if (f.type === "bool") return Object.assign(base2, { type: "checkbox", get: (c) => !!c.settings[f.name] });
    if (f.type === "choice") return Object.assign(base2, { type: "select", options: f.choices, get });
    if (f.type === "int" || f.type === "float") return Object.assign(base2, { type: "number", step: f.type === "float" ? "any" : "1", get });
    return Object.assign(base2, { mono: /url|host|path|topic|id$/.test(f.name), get });
  }
  function renderSettings() {
    if (!settingsPanel || !started.has("settings")) return;
    const fields = S.fields.map(settingField);
    const conn = fields.filter((f) => f.card === "connection");
    const behaviour = fields.filter((f) => f.card === "behaviour");
    const safety = fields.filter((f) => f.card === "safety");
    const nameField = { key: "name", label: "Name", required: true, get: (c) => c.name, help: "How it is called everywhere, e.g. in the sidebar." };
    const st = S.status;
    const side = el("div", { class: "col-side", style: { display: "flex", flexDirection: "column", gap: "18px" } },
      el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "Status")),
        el("dl", { class: "facts" },
          el("dt", {}, "Last check"), el("dd", {}, st.checked_at ? fmt.ago(new Date(st.checked_at * 1000).toISOString()) + " · " + (st.label || st.state) : "not checked yet"),
          st.has ? [el("dt", {}, "Has"), el("dd", {}, st.has)] : null,
          st.detail ? [el("dt", {}, "Detail"), el("dd", { class: st.state === "fail" ? "field-error" : null }, st.detail)] : null,
          el("dt", {}, "Last change"), el("dd", {}, S.last_change ? fmt.ago(S.last_change.at) + " by " + S.last_change.actor : "never, here"),
          el("dt", {}, "Id"), el("dd", { class: "mono" }, S.id)),
        el("div", { style: { marginTop: "12px" } }, el("button", { type: "button", class: "secondary small", onclick: testNow }, "Test now"))),
      S.from_env ? null : el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "Remove")),
        el("p", { class: "faint", style: { margin: "0 0 12px", fontSize: "13px" } }, S.used_by.length
          ? S.used_by.length + (S.used_by.length === 1 ? " service uses" : " services use") + " this source. Move them to another " + S.kind_title + " first, or switch it off to stop using it without losing its settings."
          : "Nothing uses this source. Switching it off keeps its settings."),
        el("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap" } },
          el("button", { type: "button", class: "secondary small", onclick: () => setEnabled(!S.enabled) }, S.enabled ? "Switch off" : "Switch on"),
          el("button", { type: "button", class: "danger small", onclick: remove }, "Remove…"))));
    const main = el("div", { class: "col-main stack" },
      cardFor("Connection", [nameField, ...conn], { cols: true }),
      behaviour.length || S.multi ? cardFor("Behaviour", [...behaviour, ...(S.multi ? [{ key: "default", label: "Default for new services", type: "checkbox", readonly: S.default, local: true,
        help: S.default ? "A service that names no " + S.kind_title + " uses this one. Make another one the default to change it." : "A service that names no " + S.kind_title + " uses the default one.",
        get: (c) => !!c.default }] : [])], { cols: true, onSave: async (now, was) => { if (now.default && !was.default) await api.post("/api/connections/" + enc(ID) + "/default", {}, { suppressToast: true }); } }) : null,
      safety.length ? cardFor("Safety", safety, { cols: true }) : null,
      cardFor("Where it lives", [
        { key: "site", label: "Site", placeholder: "home, offsite…", get: (c) => c.site || "" },
        { key: "scope", label: "Scope", type: "select", options: [{ value: "local", label: "Local: only this orchestrator" }, { value: "shared", label: "Shared: every orchestrator" }], get: (c) => c.scope || "local" },
        { key: "notes", label: "Notes", get: (c) => c.notes || "" },
      ], { cols: true }));
    fill(settingsPanel, el("div", { class: "grid-2" }, main, side));
  }

  // --- raw -----------------------------------------------------------------------------------------
  function raw(p) {
    if (!S.raw.length) { fill(p, el("div", { class: "card" }, el("div", { class: "dt-empty" }, el("strong", {}, "Nothing raw"), el("div", {}, "This kind shows no raw endpoints.")))); return; }
    if (!S.enabled) { off(p); return; }
    const counts = {}; const cache = {};
    let current = new URLSearchParams(location.search).get("raw") || S.raw[0].key;
    if (!S.raw.some((r) => r.key === current)) current = S.raw[0].key;
    let mode = "table"; let q = "";
    const list = el("div", { class: "raw-list", role: "list" });
    const head = el("div", { class: "card-head" });
    const filter = el("input", { type: "search", placeholder: "Filter records", "aria-label": "Filter records", style: { maxWidth: "260px" } });
    const age = el("span", { class: "faint", style: { fontSize: "12.5px" } });
    const out = el("div", { class: "dt-scroll" });
    const foot = el("div", { class: "faint", style: { fontSize: "12.5px", marginTop: "10px" } });
    fill(p, el("div", { class: "raw-layout" },
      el("div", { class: "card", style: { padding: "12px" } }, el("div", { class: "nav-heading", style: { padding: "4px 10px 8px" } }, "What the API has"), list),
      el("div", { class: "card", style: { minWidth: "0" } }, head,
        el("div", { style: { display: "flex", gap: "10px", alignItems: "center", flexWrap: "wrap", marginBottom: "10px" } }, filter, el("div", { style: { flex: "1" } }), age), out, foot)));
    filter.addEventListener("input", () => { q = filter.value.toLowerCase(); draw(); });

    function drawList() {
      fill(list, S.raw.map((r) => el("button", { type: "button", role: "listitem", "aria-current": String(r.key === current), onclick: () => pick(r.key) },
        el("span", { class: "mono" }, r.key), counts[r.key] != null ? el("span", { class: "faint" }, String(counts[r.key])) : null,
        el("span", { class: "faint raw-label" }, r.label !== r.key ? r.label : ""))));
    }
    function recordsOf(data) {
      if (Array.isArray(data)) return data;
      if (data && typeof data === "object") {
        const vals = Object.values(data);
        if (vals.length === 1 && Array.isArray(vals[0])) return vals[0];
        return Object.entries(data).map(([key, value]) => ({ key, value }));
      }
      return [{ value: data }];
    }
    const show = (v) => v == null ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v);
    function draw() {
      const hit = cache[current];
      const endpoint = S.raw.find((r) => r.key === current);
      const seg = el("div", { class: "seg", role: "group", "aria-label": "Show as" },
        ["table", "json"].map((m) => el("button", { type: "button", "aria-pressed": String(mode === m), onclick: () => { mode = m; draw(); } }, m === "table" ? "Table" : "JSON")));
      const copy = el("button", { type: "button", class: "secondary small", onclick: async () => {
        try { await navigator.clipboard.writeText(JSON.stringify(hit ? hit.data : null, null, 2)); toast("Copied", "ok"); } catch (_e) { toast("Could not copy", "error"); }
      } }, "Copy");
      const recs = hit ? recordsOf(hit.data) : [];
      fill(head, el("h2", { class: "mono" }, current), el("span", { class: "faint mono", style: { fontSize: "12.5px" } }, endpoint.describe + (hit ? " · " + recs.length + (recs.length === 1 ? " record" : " records") : "")),
        el("div", { style: { flex: "1" } }), seg, copy);
      if (!hit) return;
      if (hit.error) { fill(out, el("div", { class: "region-error" }, hit.error)); clear(foot); return; }
      const shown = q ? recs.filter((r) => JSON.stringify(r).toLowerCase().includes(q)) : recs;
      if (mode === "json") {
        fill(out, el("pre", { class: "raw-json" }, JSON.stringify(q ? shown : hit.data, null, 2)));
      } else {
        const keys = [];
        for (const r of shown.slice(0, 200)) for (const k of Object.keys(r && typeof r === "object" ? r : { value: r })) if (!keys.includes(k) && keys.length < 14) keys.push(k);
        fill(out, el("table", { class: "dt" }, el("thead", {}, el("tr", {}, keys.map((k) => el("th", { scope: "col" }, k)))),
          el("tbody", {}, shown.slice(0, 500).map((r) => el("tr", {}, keys.map((k, i) => {
            const v = r && typeof r === "object" ? r[k] : r;
            const s = show(v);
            return el("td", { class: (i === 0 ? "first " : "") + (typeof v === "number" ? "num" : "mono"), "data-label": i ? k : null, title: s.length > 80 ? s : null, style: { fontSize: "12.5px" } }, s.length > 80 ? s.slice(0, 80) + "…" : s);
          }))))));
      }
      fill(foot, (shown.length === recs.length ? recs.length : shown.length + " of " + recs.length) + " · the data as the API returns it, read only; credentials are hidden." + (shown.length > 500 && mode === "table" ? " The table shows the first 500; JSON has them all." : ""));
    }
    async function load(key, force) {
      if (cache[key] && !force) return;
      try {
        const r = await api.get(base + "/raw/" + enc(key), { suppressToast: true });
        cache[key] = { data: r.data, at: Date.now() };
        counts[key] = recordsOf(r.data).length;
      } catch (e) { cache[key] = { error: e.message, at: Date.now() }; }
    }
    async function pick(key, force) {
      current = key;
      const u = new URL(location.href); u.searchParams.set("raw", key); history.replaceState(history.state, "", u);
      drawList(); clear(out).append(el("div", { class: "skeleton", "aria-label": "Loading" }, el("i"), el("i"), el("i")));
      draw();
      await load(key, force);
      if (current !== key) return;
      fill(age, "fetched " + fmt.secondsAgo(cache[key].at) + " · ", el("a", { href: "#", onclick: (ev) => { ev.preventDefault(); pick(key, true); } }, "Refresh"));
      drawList(); draw();
    }
    pick(current);
  }

  renderHead();
  tabCtl = tabs($("#src-tabs"), { onChange: start });
  if (S.enabled && !S.status.checked_at) testNow(true);
  window.SourcePage = { get source() { return S; }, reload, setCount };
})();
