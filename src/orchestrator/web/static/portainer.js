/* The Portainer page: stacks (with what is linked to them), containers, and a log viewer.
 * Built with textContent only; nothing from Portainer is ever put in as HTML. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { stacks: [], selected: null, logTarget: null, timer: null, lines: [] };

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  const badge = (text, kind) => el("span", "badge " + kind, text);
  function stateBadge(s) {
    return badge(s, s === "running" || s === "active" || s === "up" ? "healthy"
      : s === "exited" || s === "inactive" || s === "down" || s === "dead" ? "failed" : "unknown");
  }
  function link(url, label) {
    const a = el("a", "", label);
    a.href = url; a.target = "_blank"; a.rel = "noopener";
    return a;
  }
  const linked = (slugs) => {
    const td = el("td");
    if (!slugs || !slugs.length) { td.append(el("span", "muted", "not linked")); return td; }
    slugs.forEach((s, i) => { if (i) td.append(document.createTextNode(", ")); const a = el("a", "", s); a.href = "/services/" + encodeURIComponent(s); td.append(a); });
    return td;
  };
  const err = (e) => (e && e.message) || String(e);

  // -- stacks ----------------------------------------------------------------------------
  function gitText(g) {
    if (!g) return "";
    const ref = (g.reference || "").replace("refs/heads/", "");
    return (ref ? ref : "") + (g.hash ? " @ " + g.hash.slice(0, 7) : "");
  }
  function renderStacks() {
    const q = $("p-search").value.trim().toLowerCase();
    const rows = state.stacks.filter((s) => !q || s.name.toLowerCase().includes(q));
    const body = $("stack-body");
    if (!rows.length) { const tr = el("tr"); const td = el("td", "muted", "No stacks."); td.colSpan = 9; tr.append(td); body.replaceChildren(tr); return; }
    body.replaceChildren(...rows.map((s) => {
      const tr = el("tr", "pick" + (state.selected && state.selected.name === s.name && state.selected.endpoint_id === s.endpoint_id ? " sel" : ""));
      const name = el("td"); name.append(el("strong", "", s.name));
      if (!s.managed) name.append(document.createTextNode(" "), el("span", "chip", "compose project, not in Portainer's stack list"));
      const kind = el("td"); kind.append(el("span", "chip", s.kind || "unknown"));
      const status = el("td"); status.append(stateBadge(s.status));
      const git = el("td", "", gitText(s.git)); if (s.git) git.title = s.git.url + (s.git.has_credentials ? " (uses credentials)" : "");
      const act = el("td"); if (s.open_url) act.append(link(s.open_url, "Open in Portainer"));
      tr.append(name, el("td", "", String(s.endpoint_id)), status, kind,
        el("td", "", `${s.containers_running ?? 0} / ${s.containers_total ?? 0} running`),
        git, el("td", "", s.updated_at ? fmtTime(s.updated_at) : ""), linked(s.linked_services), act);
      tr.addEventListener("click", (ev) => { if (ev.target.tagName !== "A") select(s); });
      return tr;
    }));
  }

  async function loadStacks() {
    $("p-errors").textContent = "";
    const env = $("p-env").value;
    try {
      const r = await apiFetch("GET", "/api/portainer/stacks" + (env ? "?endpoint=" + env : ""), undefined, { suppressToast: true });
      state.stacks = r.stacks;
      renderStacks();
      const problems = Object.entries(r.errors || {}).map(([k, v]) => `environment ${k}: ${v}`);
      $("p-errors").textContent = problems.join("; ");
    } catch (e) {
      state.stacks = []; renderStacks();
      $("p-errors").textContent = err(e);
    }
  }

  async function loadStatus() {
    try {
      const s = await apiFetch("GET", "/api/portainer/status", undefined, { suppressToast: true });
      $("p-status").textContent = s.detail;
      if (s.url) { $("p-open").href = s.url; $("p-open").hidden = false; }
      if (!s.ok) return false;
      const envs = await apiFetch("GET", "/api/portainer/environments", undefined, { suppressToast: true });
      const sel = $("p-env");
      sel.replaceChildren(new Option("all", ""), ...envs.map((e) => new Option(`${e.name} (${e.type}, ${e.status})`, String(e.id))));
      return true;
    } catch (e) { $("p-status").textContent = err(e); return false; }
  }

  // -- one stack ----------------------------------------------------------------------------
  async function select(s) {
    state.selected = s;
    renderStacks();
    $("detail").hidden = false;
    $("d-title").textContent = s.name;
    $("d-meta").textContent = "Loading…";
    try {
      const d = await apiFetch("GET", `/api/portainer/endpoints/${s.endpoint_id}/stacks/${encodeURIComponent(s.name)}`, undefined, { suppressToast: true });
      const bits = [`${d.type} stack`, d.status, `kind: ${d.kind}`, d.managed ? "" : "not created through Portainer",
        d.auto_update ? "auto-update on" : "", d.has_webhook ? "has a deploy webhook (id hidden)" : "",
        d.git ? `from ${d.git.url} (${gitText(d.git)})` : "", d.updated_by ? `last updated by ${d.updated_by}` : ""].filter(Boolean);
      $("d-meta").textContent = bits.join(" · ");
      $("d-env").textContent = d.env_names && d.env_names.length
        ? "Environment variables (names only, values are never read out): " + d.env_names.join(", ") : "";
      $("d-cmds").textContent = (d.host_commands || []).join("\n");
      $("cont-body").replaceChildren(...d.containers.map((c) => {
        const tr = el("tr");
        const name = el("td"); name.append(el("strong", "", c.name)); if (c.service) name.append(document.createTextNode(" "), el("span", "chip", c.service));
        const st = el("td"); st.append(stateBadge(c.state));
        const hl = el("td"); if (c.health) hl.append(badge(c.health, c.health === "healthy" ? "healthy" : c.health === "unhealthy" ? "failed" : "unknown"));
        const ports = c.ports.filter((p) => p.public).map((p) => `${p.public}→${p.private}/${p.proto}`).join(", ");
        const act = el("td");
        const logs = el("button", "secondary small", "Logs"); logs.type = "button";
        logs.addEventListener("click", () => openLogs(s.endpoint_id, c));
        act.append(logs);
        if (c.open_url) act.append(document.createTextNode(" "), link(c.open_url, "Portainer"));
        tr.append(name, st, hl, el("td", "", c.image), el("td", "", ports), linked(c.linked_services), act);
        return tr;
      }));
      $("d-meta").title = "";
    } catch (e) { $("d-meta").textContent = err(e); $("cont-body").replaceChildren(); }
    $("detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // -- logs -----------------------------------------------------------------------------------
  function renderLogs() {
    const f = $("l-filter").value.toLowerCase();
    const out = $("l-out");
    const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 24;
    const frag = document.createDocumentFragment();
    for (const l of state.lines) {
      if (f && !l.text.toLowerCase().includes(f)) continue;
      const row = el("div", l.stream === "stderr" ? "err" : "");
      if (l.ts && $("l-ts").checked) row.append(el("span", "ts", fmtTime(l.ts) + "  "));
      row.append(document.createTextNode(l.text));
      frag.append(row);
    }
    out.replaceChildren(frag);
    if (stick) out.scrollTop = out.scrollHeight;
    out.classList.toggle("wrap", $("l-wrap").checked);
  }
  async function loadLogs() {
    const t = state.logTarget;
    if (!t) return;
    const url = `/api/portainer/endpoints/${t.endpoint}/containers/${encodeURIComponent(t.name)}/logs?tail=${$("l-tail").value}&timestamps=true`;
    try {
      const r = await apiFetch("GET", url, undefined, { suppressToast: true });
      state.lines = r.lines;
      $("l-msg").textContent = `${r.count} line(s)`;
      renderLogs();
    } catch (e) { $("l-msg").textContent = err(e); }
  }
  function openLogs(endpoint, c) {
    state.logTarget = { endpoint, name: c.name };
    $("logs").hidden = false;
    $("l-name").textContent = c.name;
    loadLogs();
    $("logs").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  function setFollow() {
    clearInterval(state.timer);
    if ($("l-follow").checked) state.timer = setInterval(loadLogs, 3000);
  }
  $("l-follow").addEventListener("change", setFollow);
  $("l-reload").addEventListener("click", loadLogs);
  $("l-tail").addEventListener("change", loadLogs);
  for (const id of ["l-ts", "l-wrap", "l-filter"]) $(id).addEventListener("input", renderLogs);
  $("l-download").addEventListener("click", () => {
    const text = state.lines.map((l) => (l.ts ? l.ts + " " : "") + l.text).join("\n") + "\n";
    const a = el("a"); a.href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    a.download = (state.logTarget ? state.logTarget.name : "container") + ".log"; a.click(); URL.revokeObjectURL(a.href);
  });

  // -- wiring ---------------------------------------------------------------------------------
  $("p-env").addEventListener("change", loadStacks);
  $("p-search").addEventListener("input", renderStacks);
  $("p-refresh").addEventListener("click", async () => { await loadStacks(); if (state.selected) select(state.selected); });
  (async () => { if (await loadStatus()) loadStacks(); else renderStacks(); })();
})();
