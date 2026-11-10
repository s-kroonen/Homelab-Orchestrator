/* The drawer that adds or edits a connection, built from its kind's fields. Shared by Sources
 * (Add source), a source's Settings tab, the alert channels on Notifications and the SSH card on
 * System. Every save goes through /api/connections (validated, encrypted, audited, applied at
 * once).
 *
 *   ConnDrawer.open({ kind, conn, onSaved })   kind: {kind, title, blurb, multi, fields}
 *   ConnDrawer.remove(conn, onDone)            delete, asking again when something uses it
 *   ConnDrawer.test(id)                        -> {ok, detail, ms}
 */
(function () {
  "use strict";
  const { el, api, drawer, confirm } = window.UI;
  const toast = (m, k) => window.toast(m, k);
  const enc = encodeURIComponent;

  function fieldRows(spec, c, values) {
    const out = [];
    for (const f of spec.fields) {
      const key = "s_" + f.name;
      const isSet = c && c.secrets_set && c.secrets_set[f.name];
      if (f.type === "bool") { values[key] = c ? !!c.settings[f.name] : !!f.default; out.push({ key, label: f.label, type: "checkbox", help: f.help || null }); continue; }
      if (f.type === "choice") { values[key] = c ? c.settings[f.name] : f.default; out.push({ key, label: f.label, type: "select", options: f.choices, help: f.help || null }); continue; }
      if (f.type === "secret") {
        values[key] = "";
        out.push({ key, label: f.label, type: "password", required: f.required && !isSet, placeholder: isSet ? "set: leave blank to keep" : "", help: (f.help ? f.help + " " : "") + "Stored encrypted; never shown again." });
        continue;
      }
      values[key] = c ? (c.settings[f.name] ?? "") : (f.default ?? "");
      out.push({ key, label: f.label, type: f.type === "int" || f.type === "float" ? "number" : "text", step: f.type === "float" ? "any" : null, required: f.required, mono: /url|host|path|topic/.test(f.name), help: f.help || null });
    }
    return out;
  }

  function collect(spec, v) {
    const settings = {}; const secrets = {};
    for (const f of spec.fields) {
      const val = v["s_" + f.name];
      if (f.type === "secret") secrets[f.name] = val || "";
      else settings[f.name] = f.type === "bool" ? !!val : val == null ? "" : val;
    }
    return { settings, secrets };
  }

  async function test(id) {
    return api.post("/api/connections/" + enc(id) + "/test", {}, { suppressToast: true });
  }

  async function remove(c, onDone) {
    if (!await confirm({ title: "Remove " + (c.name || c.id) + "?", body: "Anything that uses it stops working until you point it elsewhere.", typeName: c.id, action: "Remove" })) return false;
    try { await api.del("/api/connections/" + enc(c.id), { suppressToast: true }); }
    catch (e) {
      if (!/use\(s\) this connection/.test(e.message)) { toast(e.message, "error"); return false; }
      if (!await confirm({ title: "It is still in use", body: e.message, action: "Remove anyway" })) return false;
      try { await api.del("/api/connections/" + enc(c.id) + "?force=true"); } catch (_e) { return false; }
    }
    toast("Removed " + c.id, "ok");
    if (onDone) onDone();
    return true;
  }

  function open(o) {
    const spec = o.kind; const c = o.conn || null; const editing = !!c;
    const values = { name: editing ? c.name : spec.title, id: "", enabled: editing ? c.enabled : true, default: false,
      site: editing ? c.site : "", scope: editing ? c.scope : "local", notes: editing ? c.notes : "" };
    const fields = [
      { key: "name", label: "Name", required: true, help: "How it is called everywhere, e.g. in the sidebar." },
      editing ? null : { key: "id", label: "Id", mono: true, placeholder: "made from the name", help: "How services and the CLI refer to it. Lower case, digits, - and _." },
      { key: "enabled", label: "Switched on", type: "checkbox" },
      spec.multi && !editing ? { key: "default", label: "Make it the default " + spec.title, type: "checkbox", help: "Used where nothing names one." } : null,
      { type: "section", label: spec.title },
      ...fieldRows(spec, c, values),
      { type: "section", label: "Where it lives" },
      { key: "site", label: "Site", placeholder: "home, offsite…" },
      { key: "scope", label: "Scope", type: "select", options: [{ value: "local", label: "Local: only this orchestrator" }, { value: "shared", label: "Shared: every orchestrator" }] },
      { key: "notes", label: "Notes" },
    ];
    return drawer({
      title: editing ? "Edit " + c.name : "Add " + spec.title, sub: spec.blurb, values,
      fields: fields.filter(Boolean),
      saveLabel: editing ? "Save" : "Add",
      note: "Applied at once, no restart.",
      testLabel: spec.channel ? "Send a test" : "Test",
      async onTest(v) {
        const r = await api.post("/api/connections/test", Object.assign({ kind: spec.kind, existing_id: editing ? c.id : "" }, collect(spec, v)), { suppressToast: true });
        const text = r.detail + (r.ms != null ? " (" + r.ms + " ms)" : "");
        if (r.ok === false) throw new Error(text);
        return text;
      },
      onDelete: editing && c.source !== "env" ? async () => { await remove(c, o.onRemoved); return false; } : null,
      async onSave(v) {
        const common = Object.assign({ name: v.name, enabled: !!v.enabled, site: v.site || "", scope: v.scope, notes: v.notes || "" }, collect(spec, v));
        const view = editing
          ? await api.put("/api/connections/" + enc(c.id), common, { suppressToast: true })
          : await api.post("/api/connections", Object.assign(common, { kind: spec.kind, id: (v.id || "").trim(), default: !!v.default }), { suppressToast: true });
        toast("Saved and applied", "ok");
        const saved = editing ? view.connections.find((x) => x.id === c.id) : view.connections.filter((x) => x.kind === spec.kind && x.name === v.name).pop();
        if (o.onSaved) o.onSaved(saved, view);
      },
    });
  }

  window.ConnDrawer = { open, remove, test, collect };
})();
