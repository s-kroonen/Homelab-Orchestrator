/* The "Dumps" section of the service page's "On / off & deploys" tab: how this service's own
 * database is dumped, the run button, the files (download / protect / delete). Built with
 * textContent only. Relies on SERVICE, apiFetch, toast and fmtTime from the page. */
(function () {
  "use strict";
  if (typeof SERVICE === "undefined" || !document.getElementById("dumps")) return;
  const slug = SERVICE.slug;
  const base = "/api/services/" + encodeURIComponent(slug);
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const err = (e) => (e && e.message) || String(e);
  const msg = (id, text, kind) => { const m = $(id); m.textContent = text; m.className = "msg" + (kind ? " " + kind : ""); };
  const ctl = SERVICE.controller || {};
  const hasContainer = ctl.type === "portainer" && !!ctl.container;
  const has = SERVICE.dump && Object.keys(SERVICE.dump).length > 0;
  if (!hasContainer && !has) return; // nothing here can be dumped
  $("dumps").hidden = false;

  function human(n) {
    if (n < 1024) return n + " B";
    const u = ["KB", "MB", "GB", "TB"]; let v = n / 1024, i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toFixed(1) + " " + u[i];
  }

  function syncType() {
    const t = $("du-type").value;
    const sql = ["postgres", "mysql", "mariadb", "mongo"].includes(t);
    $("du-db").hidden = $("du-db-label").hidden = !sql;
    $("du-path").hidden = $("du-path-label").hidden = t !== "sqlite";
    $("du-command").hidden = $("du-command-label").hidden = t !== "command";
  }
  $("du-type").addEventListener("change", syncType);

  function fill(cfg) {
    $("du-type").value = cfg.type || "postgres";
    $("du-db").value = cfg.database || "";
    $("du-path").value = cfg.path || "";
    $("du-command").value = cfg.command || "";
    $("du-container").value = cfg.container || "";
    $("du-schedule").value = cfg.schedule || "";
    $("du-keep").value = cfg.keep || 7;
    $("du-days").value = cfg.keep_days || 0;
    $("du-enabled").checked = cfg.enabled !== false;
    $("du-remove").hidden = !(cfg && Object.keys(cfg).length);
    syncType();
  }
  function read() {
    const t = $("du-type").value;
    const cfg = { type: t, schedule: $("du-schedule").value.trim(), keep: Number($("du-keep").value) || 7,
      keep_days: Number($("du-days").value) || 0, enabled: $("du-enabled").checked };
    if (["postgres", "mysql", "mariadb", "mongo"].includes(t) && $("du-db").value.trim()) cfg.database = $("du-db").value.trim();
    if (t === "sqlite") cfg.path = $("du-path").value.trim();
    if (t === "command") cfg.command = $("du-command").value.trim();
    if ($("du-container").value.trim()) cfg.container = $("du-container").value.trim();
    return cfg;
  }
  async function put(dump) {
    return apiFetch("PUT", "/api/registry/services/" + encodeURIComponent(slug), { dump }, { suppressToast: true });
  }
  $("du-save").addEventListener("click", async () => {
    try { const r = await put(read()); SERVICE.dump = r.dump; msg("du-msg", "Applied. Save to YAML to keep it.", "ok"); fill(r.dump); await load(); }
    catch (e) { msg("du-msg", err(e), "error"); }
  });
  $("du-remove").addEventListener("click", async () => {
    if (!confirm("Remove the dump settings? Files already taken are kept.")) return;
    try { const r = await put({}); SERVICE.dump = r.dump; fill({}); msg("du-msg", "Removed.", "ok"); await load(); }
    catch (e) { msg("du-msg", err(e), "error"); }
  });

  function steps(list) {
    const ol = $("du-steps");
    ol.hidden = !list || !list.length;
    ol.replaceChildren(...(list || []).map((s) => {
      const d = s.detail || {};
      const note = d.error || d.note || (d.size ? human(d.size) : "") || (d.removed && d.removed.length ? "removed " + d.removed.length : "");
      const li = el("li", s.status === "failed" ? "failed" : "ok", s.name + (note ? " — " + note : ""));
      return li;
    }));
  }

  let watching = false;
  async function follow(runId) {
    if (watching) return; watching = true;
    try {
      for (let i = 0; i < 1800; i++) {
        const r = await apiFetch("GET", "/api/runs/" + runId, undefined, { suppressToast: true });
        steps(r.steps);
        if (r.status !== "running") {
          msg("du-run-msg", r.status === "succeeded" ? "Dump finished." : "Dump failed: " + (r.error || r.status), r.status === "succeeded" ? "ok" : "error");
          break;
        }
        await new Promise((res) => setTimeout(res, 1000));
      }
    } finally { watching = false; $("du-run").disabled = false; load(); }
  }
  $("du-run").addEventListener("click", async () => {
    $("du-run").disabled = true; msg("du-run-msg", "Running…");
    try { const r = await apiFetch("POST", base + "/dump/run", undefined, { suppressToast: true }); follow(r.run_id); }
    catch (e) { msg("du-run-msg", err(e), "error"); $("du-run").disabled = false; }
  });

  function fileRow(r) {
    const tr = el("tr");
    tr.append(el("td", "", fmtTime(r.created_at)), el("td", "", human(r.size_bytes)),
      el("td", "", (r.verified ? "complete" : "not checked") + (r.protected ? ", protected" : "")));
    const act = el("td");
    const dl = el("a", "btn small secondary", "Download"); dl.href = "/api/dumps/" + r.id + "/download"; dl.download = r.file;
    const pr = el("button", "secondary small", r.protected ? "Unprotect" : "Protect");
    pr.addEventListener("click", async () => {
      try { await apiFetch("POST", "/api/dumps/" + r.id + "/protect", { protected: !r.protected }, { suppressToast: true }); load(); } catch (e) { toast(err(e), "error"); }
    });
    const del = el("button", "danger small", "Delete");
    del.addEventListener("click", async () => {
      if (!confirm("Delete " + r.file + "?")) return;
      try { await apiFetch("DELETE", "/api/dumps/" + r.id, undefined, { suppressToast: true }); load(); } catch (e) { toast(err(e), "error"); }
    });
    act.append(dl, " ", pr, " ", del);
    tr.append(act);
    return tr;
  }

  async function load() {
    let d;
    try { d = await apiFetch("GET", base + "/dump", undefined, { suppressToast: true }); }
    catch (e) { msg("du-msg", err(e), "error"); return; }
    $("du-intro").textContent = "The database's own dump tool runs inside its container (through Portainer), the output is compressed " +
      "on the orchestrator host, checked, and the newest few are kept. Credentials come from the container's own environment.";
    $("du-actions").hidden = !d.configured;
    const badge = $("du-state");
    badge.hidden = !d.configured;
    if (!d.configured) return;
    const last = (d.records || []).find((r) => r.verified);
    badge.className = "badge " + (last ? "healthy" : "unknown");
    badge.textContent = last ? "last " + fmtTime(last.created_at) : "never dumped";
    $("du-where").textContent = "Kept in " + d.directory + (d.config.schedule ? "; scheduled: " + d.config.schedule : "; only when asked") + ".";
    $("du-restore").textContent = d.restore_hint || "";
    $("du-files").replaceChildren(...((d.records || []).length ? d.records.map(fileRow) : [(() => { const tr = el("tr"); const td = el("td", "muted", "No dumps yet."); td.colSpan = 4; tr.append(td); return tr; })()]));
    if (d.last_run && d.last_run.status === "running") follow(d.last_run.run_id);
    else if (d.last_run) steps(d.last_run.steps);
  }
  fill(SERVICE.dump || {});
  load();
  if (window.OrchLive) OrchLive.on(["run"], (ev) => { if (ev.kind === "dump") load(); }, { wait: 800 });
})();
