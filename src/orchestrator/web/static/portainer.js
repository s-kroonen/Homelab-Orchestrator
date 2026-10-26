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
      const problems = Object.entries(r.errors || {}).map(([k, v]) => `environment ${k} (${v})`);
      $("p-errors").textContent = problems.length
        ? `${problems.length} environment(s) are unreachable, so they and their stacks are not shown: ${problems.join("; ")}`
        : "";
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
      // a down environment is not offered: its stacks are not shown, so there is nothing to pick
      sel.replaceChildren(new Option("all", ""), ...envs.filter((e) => e.status !== "down").map((e) => new Option(`${e.name} (${e.type}, ${e.status})`, String(e.id))));
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
      state.detail = d;
      $("d-adopt").hidden = !!(d.linked_services && d.linked_services.length);
      $("d-msg").textContent = "";
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
        for (const verb of ["start", "stop", "restart"]) {
          const b = el("button", "secondary small", verb[0].toUpperCase() + verb.slice(1)); b.type = "button";
          b.addEventListener("click", () => containerAction(s, c, verb));
          act.append(document.createTextNode(" "), b);
        }
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


  // -- control ----------------------------------------------------------------------------------
  async function containerAction(s, c, verb) {
    if (verb !== "start" && !confirm(`${verb} container ${c.name}?`)) return;
    try {
      const r = await apiFetch("POST", `/api/containers/${s.endpoint_id}/${encodeURIComponent(c.name)}/${verb}`, undefined, { suppressToast: true });
      $("d-msg").textContent = r.message; $("d-msg").className = "msg ok";
    } catch (e) { $("d-msg").textContent = err(e); $("d-msg").className = "msg error"; }
    setTimeout(() => select(s), 800);
  }
  for (const b of document.querySelectorAll("[data-stack-action]")) {
    b.addEventListener("click", async () => {
      const s = state.selected; if (!s) return;
      const verb = b.dataset.stackAction;
      if (verb !== "start" && !confirm(`${verb} stack ${s.name}?`)) return;
      try {
        const r = await apiFetch("POST", `/api/stacks/${s.endpoint_id}/${encodeURIComponent(s.name)}/${verb}`, undefined, { suppressToast: true });
        $("d-msg").textContent = r.message; $("d-msg").className = "msg ok";
      } catch (e) { $("d-msg").textContent = err(e); $("d-msg").className = "msg error"; }
      setTimeout(async () => { await loadStacks(); select(s); }, 800);
    });
  }

  // -- panels: only one of adopt / create / delete is open at a time ------------------------------
  function openPanel(id) {
    for (const p of ["create", "delete"]) $(p).hidden = p !== id;
    $(id).scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  const closePanels = () => { for (const p of ["create", "delete"]) $(p).hidden = true; };
  const option = (value, text) => new Option(text, value);

  // -- create ----------------------------------------------------------------------------------------
  let previewed = null;
  function addVar(name, value) {
    const row = el("div", "var-row");
    const k = el("input"); k.type = "text"; k.placeholder = "NAME"; k.value = name || ""; k.autocomplete = "off";
    const v = el("input"); v.type = "password"; v.placeholder = "value (not kept)"; v.value = value || ""; v.autocomplete = "new-password";
    const rm = el("button", "secondary small", "Remove"); rm.type = "button"; rm.addEventListener("click", () => { row.remove(); invalidate(); });
    k.addEventListener("input", invalidate); v.addEventListener("input", invalidate);
    row.append(k, v, rm); $("c-vars").append(row);
  }
  function invalidate() { previewed = null; $("c-go").disabled = true; $("c-request").hidden = true; }
  function createBody() {
    const env = {};
    for (const row of $("c-vars").children) {
      const [k, v] = row.querySelectorAll("input");
      if (k.value.trim()) env[k.value.trim()] = v.value;
    }
    const git = $("c-source").value === "git";
    return { endpoint_id: parseInt($("c-env").value, 10), name: $("c-name").value.trim(), source: $("c-source").value,
      repository_url: git ? $("c-url").value.trim() : "", reference: $("c-ref").value.trim(), compose_path: $("c-path").value.trim(),
      git_credential_id: git && $("c-cred").value ? parseInt($("c-cred").value, 10) : null,
      git_username: git ? $("c-user").value : "", git_password: git ? $("c-pass").value : "",
      auto_update: git && $("c-auto").checked, compose_text: git ? "" : $("c-compose").value, env };
  }
  async function openCreate() {
    let envs = [];
    try { envs = await apiFetch("GET", "/api/portainer/environments", undefined, { suppressToast: true }); } catch (e) { /* the panel still opens */ }
    const docker = envs.filter((e) => ["docker", "agent", "edge"].includes(e.type));
    $("c-env").replaceChildren(...docker.map((e) => option(String(e.id), `${e.name} (${e.status})`)));
    let parents = [];
    try { parents = (await apiFetch("GET", "/api/stacks/parents", undefined, { suppressToast: true })).parents; } catch (e) { /* none to offer */ }
    $("c-parent").replaceChildren(...parents.map((p) => option(p.slug, `${p.name} (${p.guest})`)));
    if (!$("c-vars").children.length) addVar();
    invalidate(); $("c-msg").textContent = ""; openPanel("create");
  }
  $("p-new").addEventListener("click", openCreate);
  $("c-cancel").addEventListener("click", closePanels);
  $("c-addvar").addEventListener("click", () => addVar());
  $("c-source").addEventListener("change", () => { const git = $("c-source").value === "git"; $("c-git").hidden = !git; $("c-text").hidden = git; invalidate(); });
  for (const id of ["c-env", "c-name", "c-url", "c-ref", "c-path", "c-cred", "c-user", "c-pass", "c-auto", "c-compose"]) $(id).addEventListener("input", invalidate);
  $("c-preview").addEventListener("click", async () => {
    try {
      previewed = await apiFetch("POST", "/api/stacks/preview", createBody(), { suppressToast: true });
      $("c-request").textContent = JSON.stringify(previewed, null, 2); $("c-request").hidden = false;
      $("c-go").disabled = false; $("c-msg").textContent = "This is what will be sent to Portainer. Secrets are masked."; $("c-msg").className = "msg";
    } catch (e) { invalidate(); $("c-msg").textContent = err(e); $("c-msg").className = "msg error"; }
  });
  $("c-go").addEventListener("click", async () => {
    if (!previewed) return;
    const body = { spec: createBody(), confirm: true };
    if ($("c-adopt").checked && $("c-slug").value.trim()) {
      body.adopt = { endpoint_id: body.spec.endpoint_id, stack: body.spec.name, slug: $("c-slug").value.trim(), parent: $("c-parent").value };
    }
    $("c-go").disabled = true; $("c-msg").textContent = "Creating…";
    try {
      const r = await apiFetch("POST", "/api/stacks", body, { suppressToast: true });
      $("c-msg").textContent = `Created ${r.stack.name} in Portainer.` + (r.warnings.length ? " " + r.warnings.join(" ") : "") + (r.adopted ? " Linked as " + r.adopted.services.join(", ") + "." : " Adopt it to link it here.");
      $("c-msg").className = "msg ok";
      for (const row of $("c-vars").children) row.querySelectorAll("input")[1].value = "";
      $("c-pass").value = "";
      loadStacks();
    } catch (e) { $("c-msg").textContent = err(e); $("c-msg").className = "msg error"; $("c-go").disabled = false; }
  });

  // -- delete -----------------------------------------------------------------------------------------
  $("d-delete").addEventListener("click", () => {
    const s = state.selected; if (!s) return;
    $("x-stack").textContent = s.name; $("x-name").value = ""; $("x-msg").textContent = ""; $("x-down").checked = false; $("x-keep").checked = false;
    const linkedTo = (state.detail && state.detail.linked_services) || [];
    $("x-linked").textContent = linkedTo.length ? "Linked here as: " + linkedTo.join(", ") + ". Their sub-services are removed with it unless you keep them." : "Nothing here is linked to it.";
    openPanel("delete");
  });
  $("x-cancel").addEventListener("click", closePanels);
  $("x-go").addEventListener("click", async () => {
    const s = state.selected; if (!s) return;
    try {
      const r = await apiFetch("DELETE", `/api/stacks/${s.endpoint_id}/${encodeURIComponent(s.name)}`,
        { confirm_name: $("x-name").value, take_down: $("x-down").checked, remove_services: !$("x-keep").checked }, { suppressToast: true });
      const bits = [`Deleted ${r.stack} (volumes kept).`];
      if (r.taken_down.length) bits.push("Switched off: " + r.taken_down.join(", ") + ".");
      if (r.removed_services.length) bits.push("Removed here: " + r.removed_services.join(", ") + ".");
      if (r.unlinked_hosts.length) bits.push("Proxy hosts now unlinked: " + r.unlinked_hosts.join(", ") + ".");
      if (r.warnings.length) bits.push(r.warnings.join(" "));
      $("x-msg").textContent = bits.join(" "); $("x-msg").className = "msg ok";
      state.selected = null; $("detail").hidden = true;
      loadStacks();
    } catch (e) { $("x-msg").textContent = err(e); $("x-msg").className = "msg error"; }
  });

  // -- wiring ---------------------------------------------------------------------------------
  $("p-env").addEventListener("change", loadStacks);
  $("p-search").addEventListener("input", renderStacks);
  $("p-refresh").addEventListener("click", async () => { await loadStacks(); if (state.selected) select(state.selected); });
  (async () => { if (await loadStatus()) loadStacks(); else renderStacks(); })();
})();
