/* Containers (templates/portainer.html), to the Containers wireframe: the stacks in Portainer as a
 * table (filter, chips, environment, row menu, Start / Stop / Restart for a selection); click a
 * stack to see its containers inside (Logs, Restart, ⋯), + New stack and the logs in drawers.
 * Everything is read live from Portainer through /api/portainer/*; nothing from Portainer is ever
 * put in as HTML. */
(function () {
  "use strict";
  const { $, el, clear, fill, api, fmt, region, table, drawer, confirm } = window.UI;
  const toast = (m, k) => window.toast(m, k);
  const enc = encodeURIComponent;
  const params = new URLSearchParams(location.search);
  const S = { instance: params.get("instance") || "", env: params.get("env") || "", open: new Set(), detail: new Map(), stats: new Map(), instances: [] };
  const keyOf = (s) => s.endpoint_id + "/" + s.name;
  const u = (path) => (S.instance ? path + (path.includes("?") ? "&" : "?") + "instance=" + enc(S.instance) : path);
  const stackUrl = (s, rest) => u("/api/stacks/" + s.endpoint_id + "/" + enc(s.name) + (rest || ""));

  // --- what a stack is ---------------------------------------------------------------------------
  function stateOf(s) {
    const total = s.containers_total || 0, run = s.containers_running || 0;
    if (total && run === total) return { cls: "ok", label: "running" };
    if (run > 0) return { cls: "warn", label: "partly running" };
    if (s.status === "active" && !total) return { cls: "off", label: "no containers" };
    return { cls: "off", label: "stopped" };
  }
  const gitText = (g) => (g ? "git " + ((g.reference || "").replace("refs/heads/", "") || "?") + (g.hash ? " @ " + g.hash.slice(0, 7) : "") : "");
  const sourceText = (s) => (s.git ? gitText(s.git) : !s.managed ? "compose project" : s.kind || "");
  const linkedTo = (slugs) => slugs.map((x, i) => [i ? ", " : "", el("a", { href: "/services/" + enc(x) }, x)]);
  const cpill = (st) => el("span", { class: "pill " + (st === "running" ? "ok" : st === "restarting" || st === "paused" ? "warn" : st === "dead" ? "fail" : "off") }, st);

  // --- actions -----------------------------------------------------------------------------------
  async function stackAction(rows, verb) {
    if (verb !== "start" && !await confirm({ title: (verb === "stop" ? "Stop " : "Restart ") + (rows.length === 1 ? rows[0].name : rows.length + " stacks") + "?",
      body: verb === "stop" ? "Their containers stop; services that use them stop answering." : "Their containers restart; anyone using them is cut off for a moment.",
      affectedLabel: rows.length > 1 ? "Stacks" : null, affected: rows.length > 1 ? rows.map((r) => r.name) : null, action: verb === "stop" ? "Stop" : "Restart", danger: verb === "stop" })) return;
    for (const s of rows) {
      try { const r = await api.post(stackUrl(s, "/" + verb), undefined, { suppressToast: true }); toast(r.message || s.name + ": " + verb + " sent", "ok"); }
      catch (e) { toast(s.name + ": " + e.message, "error"); }
    }
    t.clearSelection();
    setTimeout(() => { for (const s of rows) S.detail.delete(keyOf(s)); view.refresh(); }, 800);
  }
  async function containerAction(s, c, verb) {
    if (verb !== "start" && !await confirm({ title: (verb === "stop" ? "Stop " : "Restart ") + c.name + "?", action: verb === "stop" ? "Stop" : "Restart", danger: verb === "stop" })) return;
    try { const r = await api.post(u("/api/containers/" + s.endpoint_id + "/" + enc(c.name) + "/" + verb), undefined, { suppressToast: true }); toast(r.message || c.name + ": " + verb + " sent", "ok"); }
    catch (e) { toast(e.message, "error"); }
    setTimeout(() => { S.detail.delete(keyOf(s)); loadDetail(s); view.refresh(); }, 800);
  }
  async function deleteStack(s) {
    const d = S.detail.get(keyOf(s)) || s;
    const linked = d.linked_services || [];
    const down = el("input", { type: "checkbox" }); const keep = el("input", { type: "checkbox" });
    const ok = await confirm({
      title: "Delete stack " + s.name + "?",
      body: el("div", {}, el("p", {}, "Deletes it in Portainer (its volumes are kept) and then the link here. Its containers stop."),
        el("p", {}, linked.length ? "Linked here as " + linked.join(", ") + ": their sub-services are removed with it unless you keep them." : "Nothing here is linked to it."),
        el("label", { class: "inline" }, down, " switch linked services off first, in the right order"), el("br"),
        linked.length ? el("label", { class: "inline" }, keep, " keep the sub-services (only clear the link)") : null),
      typeName: s.name, action: "Delete the stack",
    });
    if (!ok) return;
    try {
      const r = await window.apiFetch("DELETE", stackUrl(s), { confirm_name: s.name, take_down: down.checked, remove_services: !keep.checked }, { suppressToast: true });
      const bits = ["Deleted " + r.stack + " (volumes kept)."];
      if (r.taken_down.length) bits.push("Switched off: " + r.taken_down.join(", ") + ".");
      if (r.removed_services.length) bits.push("Removed here: " + r.removed_services.join(", ") + ".");
      if (r.unlinked_hosts.length) bits.push("Proxy hosts now unlinked: " + r.unlinked_hosts.join(", ") + ".");
      if (r.warnings.length) bits.push(r.warnings.join(" "));
      toast(bits.join(" "), "ok");
    } catch (e) { toast(e.message, "error"); }
    S.open.delete(keyOf(s)); view.refresh();
  }

  // --- one stack, expanded -----------------------------------------------------------------------
  async function loadDetail(s) {
    const k = keyOf(s);
    try {
      const d = await api.get(u("/api/portainer/endpoints/" + s.endpoint_id + "/stacks/" + enc(s.name)), { suppressToast: true });
      S.detail.set(k, d);
      t.render();
      await Promise.all(d.containers.filter((c) => c.state === "running").map((c) =>
        api.get(u("/api/portainer/endpoints/" + s.endpoint_id + "/containers/" + enc(c.name) + "/stats"), { suppressToast: true })
          .then((st) => S.stats.set(s.endpoint_id + "/" + c.name, st), () => {})));
      t.render();
    } catch (e) { S.detail.set(k, { error: e.message }); t.render(); }
  }
  function toggle(s) {
    const k = keyOf(s);
    if (S.open.has(k)) S.open.delete(k); else { S.open.add(k); if (!S.detail.has(k)) loadDetail(s); }
    t.render();
  }
  function detailOf(s) {
    const k = keyOf(s);
    if (!S.open.has(k)) return null;
    const d = S.detail.get(k);
    if (!d) return el("div", { class: "ct-detail faint" }, "Loading its containers…");
    if (d.error) return el("div", { class: "ct-detail" }, el("span", { class: "pill fail" }, "error"), " " + d.error);
    const meta = [d.type + " stack", d.managed ? "" : "not created through Portainer", d.auto_update ? "auto-update on" : "", d.has_webhook ? "has a deploy webhook (id hidden)" : "",
      d.git ? "from " + d.git.url + (d.git.has_credentials ? " (uses credentials)" : "") : "", d.updated_by ? "last updated by " + d.updated_by : ""].filter(Boolean).join(" · ");
    return el("div", { class: "ct-detail" },
      el("div", { class: "ct-meta" }, meta, d.env_names && d.env_names.length ? el("div", {}, "Environment variables (names only; values are never read out): " + d.env_names.join(", ")) : null),
      d.containers.length ? d.containers.map((c) => {
        const st = S.stats.get(s.endpoint_id + "/" + c.name);
        const ports = c.ports.filter((p) => p.public).map((p) => (p.ip && p.ip !== "0.0.0.0" ? p.ip + ":" : "") + p.public + "→" + p.private + (p.proto !== "tcp" ? "/" + p.proto : "")).join(", ");
        return el("div", { class: "ct-row" },
          el("span", { class: "mono", title: c.name }, c.name, c.service && c.service !== c.name ? el("span", { class: "faint" }, " · " + c.service) : null),
          el("span", { class: "mono faint", title: c.image }, c.image),
          el("span", {}, cpill(c.state), c.health ? [" ", el("span", { class: "pill " + (c.health === "healthy" ? "ok" : c.health === "unhealthy" ? "fail" : "off") }, c.health)] : null),
          el("span", { class: "mono", style: { fontSize: "12px" } }, ports || "—"),
          el("span", { class: "faint" }, st ? Math.round(st.cpu_percent) + "% · " + fmt.bytes(st.mem_bytes) : c.status_text || ""),
          el("span", { class: "acts" },
            el("button", { type: "button", class: "ghost small", onclick: () => logs(s, c) }, "Logs"),
            el("button", { type: "button", class: "ghost small", onclick: () => containerAction(s, c, "restart") }, "Restart"),
            window.UI.menuButton(() => [
              { label: "Start", onClick: () => containerAction(s, c, "start") },
              { label: "Stop", onClick: () => containerAction(s, c, "stop") },
              { label: "Restart", onClick: () => containerAction(s, c, "restart") },
              c.open_url ? { label: "Open in Portainer ↗", href: c.open_url } : null,
              c.linked_services && c.linked_services.length ? { label: "Open " + c.linked_services[0], href: "/services/" + enc(c.linked_services[0]) } : null,
            ].filter(Boolean), "Actions for " + c.name)));
      }) : el("p", { class: "faint" }, "No containers."),
      d.host_commands && d.host_commands.length ? el("details", { class: "ct-meta" }, el("summary", {}, "The same by hand, on the Docker host"), el("pre", { class: "sys-pre" }, d.host_commands.join("\n"))) : null);
  }

  // --- logs ---------------------------------------------------------------------------------------
  function logs(s, c) {
    let lines = []; let timer = 0;
    const out = el("pre", { class: "log", "aria-live": "off" });
    const tail = el("select", { "aria-label": "Lines" }, [100, 200, 500, 1000, 2000].map((n) => el("option", { value: String(n), selected: n === 200 }, n + " lines")));
    const follow = el("input", { type: "checkbox" }); const ts = el("input", { type: "checkbox", checked: true }); const wrap = el("input", { type: "checkbox" });
    const filter = el("input", { type: "search", placeholder: "filter lines", "aria-label": "Filter lines" });
    const count = el("span", { class: "faint", style: { fontSize: "12.5px" } });
    function draw() {
      const f = filter.value.toLowerCase();
      const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 24;
      fill(out, lines.filter((l) => !f || l.text.toLowerCase().includes(f)).map((l) =>
        el("div", { class: l.stream === "stderr" ? "err" : null }, l.ts && ts.checked ? el("span", { class: "ts" }, fmt.time(l.ts) + "  ") : null, l.text)));
      if (stick) out.scrollTop = out.scrollHeight;
      out.classList.toggle("wrap", wrap.checked);
    }
    async function load() {
      try {
        const r = await api.get(u("/api/portainer/endpoints/" + s.endpoint_id + "/containers/" + enc(c.name) + "/logs?tail=" + tail.value + "&timestamps=true"), { suppressToast: true });
        lines = r.lines; count.textContent = r.count + " lines"; draw();
      } catch (e) { count.textContent = e.message; }
    }
    function download() {
      const text = lines.map((l) => (l.ts ? l.ts + " " : "") + l.text).join("\n") + "\n";
      const a = el("a", { href: URL.createObjectURL(new Blob([text], { type: "text/plain" })), download: c.name + ".log" });
      a.click(); URL.revokeObjectURL(a.href);
    }
    follow.addEventListener("change", () => { clearInterval(timer); if (follow.checked) timer = setInterval(load, 3000); });
    tail.addEventListener("change", load);
    for (const x of [ts, wrap, filter]) x.addEventListener("input", draw);
    drawer({
      title: "Logs of " + c.name, sub: s.name + " · " + c.image, wide: true, saveLabel: "Done",
      async onSave() { /* nothing to save */ },
      onClose: () => clearInterval(timer),
      fields: [{ type: "custom", render: () => el("div", {},
        el("div", { class: "log-tools" }, tail, el("label", { class: "inline" }, follow, " follow"), el("label", { class: "inline" }, ts, " timestamps"),
          el("label", { class: "inline" }, wrap, " wrap"), filter,
          el("button", { type: "button", class: "secondary small", onclick: load }, "Reload"),
          el("button", { type: "button", class: "secondary small", onclick: download }, "Download"), count),
        out) }],
    });
    load();
  }

  // --- new stack ------------------------------------------------------------------------------------
  async function newStack() {
    let envs = []; let parents = [];
    try { envs = await api.get(u("/api/portainer/environments"), { suppressToast: true }); } catch (_e) { /* still open the drawer */ }
    try { parents = (await api.get("/api/stacks/parents", { suppressToast: true })).parents; } catch (_e) { /* none to offer */ }
    const docker = envs.filter((e) => ["docker", "agent", "edge"].includes(e.type));
    const vars = el("div");
    const addVar = () => vars.append(el("div", { class: "var-row" }, el("input", { type: "text", placeholder: "NAME", autocomplete: "off", "aria-label": "Name" }),
      el("input", { type: "password", placeholder: "value (not kept)", autocomplete: "new-password", "aria-label": "Value" }),
      el("button", { type: "button", class: "ghost small", onclick: (e) => e.currentTarget.parentElement.remove() }, "Remove")));
    addVar();
    let previewed = null;
    const previewBox = el("pre", { class: "sys-pre", hidden: true });
    const bodyOf = (v) => {
      const env = {};
      for (const row of vars.children) { const [k, val] = row.querySelectorAll("input"); if (k.value.trim()) env[k.value.trim()] = val.value; }
      const git = v.source === "git";
      return { endpoint_id: Number(v.endpoint), name: (v.name || "").trim(), source: v.source,
        repository_url: git ? (v.url || "").trim() : "", reference: (v.ref || "").trim(), compose_path: (v.path || "").trim(),
        git_credential_id: git && v.cred ? Number(v.cred) : null, git_username: git ? v.user || "" : "", git_password: git ? v.pass || "" : "",
        auto_update: git && !!v.auto, compose_text: git ? "" : v.compose || "", env };
    };
    drawer({
      title: "New stack",
      sub: "Sent to Portainer, which creates and owns it. Variable values and the Git password go straight to Portainer and are not kept, logged or shown again.",
      values: { endpoint: docker[0] ? String(docker[0].id) : "", name: "", source: "git", url: "", ref: "refs/heads/main", path: "docker-compose.yml", cred: "", user: "", pass: "", auto: false, compose: "", adopt: false, slug: "", parent: parents[0] ? parents[0].slug : "" },
      saveLabel: "Create in Portainer", testLabel: "Preview the request",
      note: "Preview first: it shows exactly what is sent.",
      fields: [
        { key: "endpoint", label: "Environment", type: "select", required: true, options: docker.map((e) => ({ value: String(e.id), label: e.name + " (" + e.status + ")" })) },
        { key: "name", label: "Stack name", required: true, mono: true },
        { key: "source", label: "Source", type: "select", options: [{ value: "git", label: "a Git repository" }, { value: "string", label: "compose text" }] },
        { key: "url", label: "Repository URL", mono: true, placeholder: "https://git.example.lan/group/app.git", show: (v) => v.source === "git" },
        { key: "ref", label: "Reference", mono: true, show: (v) => v.source === "git" },
        { key: "path", label: "Compose file", mono: true, show: (v) => v.source === "git" },
        { key: "cred", label: "Saved credential", type: "number", help: "A Git credential Portainer already has (its id); or a username and password below.", show: (v) => v.source === "git" },
        { key: "user", label: "Username", show: (v) => v.source === "git" },
        { key: "pass", label: "Password", type: "password", show: (v) => v.source === "git" },
        { key: "auto", label: "Let Portainer poll the repository (auto update)", type: "checkbox", show: (v) => v.source === "git" },
        { key: "compose", label: "Compose text", type: "textarea", rows: 10, mono: true, show: (v) => v.source === "string" },
        { type: "section", label: "Environment variables" },
        { type: "custom", render: () => el("div", {}, vars, el("button", { type: "button", class: "secondary small", onclick: addVar }, "Add a variable")) },
        { type: "section", label: "Link it here" },
        { key: "adopt", label: "Also link it as a sub-service", type: "checkbox" },
        { key: "slug", label: "Slug", mono: true, show: (v) => v.adopt },
        { key: "parent", label: "Under", type: "select", show: (v) => v.adopt, options: parents.map((p) => ({ value: p.slug, label: p.name + " (" + p.guest + ")" })) },
        { type: "custom", render: () => previewBox },
      ],
      async onTest(v) {
        const body = bodyOf(v);
        const req = await api.post(u("/api/stacks/preview"), body, { suppressToast: true });
        previewed = JSON.stringify(body);
        previewBox.textContent = JSON.stringify(req, null, 2);
        previewBox.hidden = false;
        previewBox.scrollIntoView({ block: "nearest" });
        return "Above: exactly what is sent to Portainer, secrets masked.";
      },
      async onSave(v) {
        const spec = bodyOf(v);
        if (previewed !== JSON.stringify(spec)) throw new Error("Preview the request first: it shows exactly what is sent to Portainer.");
        const body = { spec, confirm: true, instance: S.instance };
        if (v.adopt && (v.slug || "").trim()) body.adopt = { endpoint_id: spec.endpoint_id, stack: spec.name, slug: v.slug.trim(), parent: v.parent, instance: S.instance };
        const r = await api.post("/api/stacks", body, { suppressToast: true });
        toast("Created " + r.stack.name + " in Portainer." + (r.warnings.length ? " " + r.warnings.join(" ") : "") + (r.adopted ? " Linked as " + r.adopted.services.join(", ") + "." : ""), "ok");
        view.refresh();
      },
    });
  }
  $("#new-stack").addEventListener("click", newStack);

  // --- the table ------------------------------------------------------------------------------------
  const age = el("span", { class: "age" });
  const instSel = el("select", { "aria-label": "Portainer", hidden: true });
  const envSel = el("select", { "aria-label": "Environment" }, el("option", { value: "" }, "Environment: all"));
  const t = table($("#stacks"), {
    rows: [], key: keyOf, name: (s) => s.name, noun: ["stack", "stacks"],
    placeholder: "Filter by stack, container, image…",
    search: (s) => [s.name, sourceText(s), (s.linked_services || []).join(" "), ((S.detail.get(keyOf(s)) || {}).containers || []).map((c) => c.name + " " + c.image).join(" ")].join(" "),
    sort: { key: "name", dir: 1 },
    toolbarExtra: el("span", { style: { display: "inline-flex", gap: "8px" } }, instSel, envSel),
    age,
    columns: [
      { key: "name", label: "Stack", sort: (s) => s.name.toLowerCase(),
        render: (s) => [el("button", { type: "button", class: "ct-toggle", "aria-expanded": String(S.open.has(keyOf(s))), onclick: () => toggle(s) },
          el("span", { class: "tw", "aria-hidden": "true" }, S.open.has(keyOf(s)) ? "▾" : "▸"), s.name),
          el("span", { class: "sub" }, (S.envs && S.envs[s.endpoint_id]) || "environment " + s.endpoint_id, !s.managed ? [" · ", el("span", { class: "tag" }, "not in Portainer's stack list")] : null)] },
      { key: "state", label: "State", sort: (s) => stateOf(s).label, render: (s) => { const x = stateOf(s); return el("span", { class: "pill " + x.cls }, x.label); } },
      { key: "containers", label: "Containers", sort: (s) => s.containers_running || 0, render: (s) => (s.containers_running || 0) + " / " + (s.containers_total || 0) + " running" },
      { key: "source", label: "Source", phone: false, render: (s) => el("span", { class: "mono", style: { fontSize: "12px" }, title: s.git ? s.git.url : null }, sourceText(s)) },
      { key: "updated", label: "Updated", sort: (s) => s.updated_at || "", render: (s) => s.updated_at ? el("span", { class: "faint", title: fmt.time(s.updated_at) }, fmt.ago(s.updated_at)) : "—" },
      { key: "service", label: "Service", render: (s) => (s.linked_services || []).length ? linkedTo(s.linked_services) : [el("span", { class: "faint" }, "not linked"), " · ", el("a", { href: "/sources#portainer" }, "Link…")] },
    ],
    chips: [
      { key: "running", label: "Running", test: (s) => stateOf(s).label === "running" },
      { key: "stopped", label: "Stopped", test: (s) => stateOf(s).label === "stopped" },
      { key: "partly", label: "Partly running", test: (s) => stateOf(s).label === "partly running" },
      { key: "unlinked", label: "Not linked", test: (s) => !(s.linked_services || []).length },
    ],
    detail: detailOf,
    menu: (s) => [
      { label: S.open.has(keyOf(s)) ? "Hide its containers" : "Show its containers", onClick: () => toggle(s) },
      { label: "Start", onClick: () => stackAction([s], "start") },
      { label: "Stop", onClick: () => stackAction([s], "stop") },
      { label: "Restart", onClick: () => stackAction([s], "restart") },
      (s.linked_services || []).length ? { label: "Open " + s.linked_services[0], href: "/services/" + enc(s.linked_services[0]) } : { label: "Link to a service…", href: "/sources#portainer" },
      s.open_url ? { label: "Open in Portainer ↗", href: s.open_url } : null,
      "-",
      { label: "Delete…", danger: true, onClick: () => deleteStack(s) },
    ].filter(Boolean),
    bulk: (rows) => [
      { label: "Start", onClick: () => stackAction(rows, "start") },
      { label: "Stop", onClick: () => stackAction(rows, "stop") },
      { label: "Restart", onClick: () => stackAction(rows, "restart") },
    ],
    empty: { title: "No stacks", text: "Portainer has no stacks here yet.", action: el("button", { type: "button", class: "small", onclick: newStack }, "+ New stack") },
  });

  // --- loading -------------------------------------------------------------------------------------
  async function load() {
    const status = await api.get(u("/api/portainer/status"), { suppressToast: true });
    const banners = [];
    if (status.url) { $("#open-portainer").href = status.url; $("#open-portainer").hidden = false; }
    if (!status.ok) {
      banners.push(el("div", { class: "banner sys-banner" }, el("span", { class: "pill " + (status.configured ? "fail" : "off") }, status.configured ? "down" : "not set up"), " ",
        el("strong", {}, "Portainer" + (S.instances.length > 1 ? " " + (S.instances.find((i) => i.id === status.instance) || {}).name : "")), " " + status.detail + " ",
        el("a", { class: "btn secondary small", href: "/infra/connections" }, status.configured ? "Check connection" : "Set it up")));
      return { stacks: [], banners };
    }
    const envs = await api.get(u("/api/portainer/environments"), { suppressToast: true }).catch(() => []);
    S.envs = Object.fromEntries(envs.map((e) => [e.id, e.name]));
    const keep = envSel.value || S.env;
    fill(envSel, el("option", { value: "" }, "Environment: all"), envs.filter((e) => e.status !== "down").map((e) => el("option", { value: String(e.id) }, e.name + " (" + e.type + ")")));
    envSel.value = keep;
    const r = await api.get(u("/api/portainer/stacks" + (envSel.value ? "?endpoint=" + envSel.value : "")), { suppressToast: true });
    const down = Object.values(r.errors || {});
    if (down.length) banners.push(el("div", { class: "banner sys-banner" }, el("span", { class: "pill warn" }, "hidden"), " ",
      down.length === 1 ? "One environment is not answering, so its stacks are not shown: " : down.length + " environments are not answering, so their stacks are not shown: ", down.join("; ")));
    return { stacks: r.stacks, banners };
  }
  const view = region(el("div"), { // the table is the visible part; the section only loads
    load,
    render: (d) => {
      fill($("#banners"), d.banners);
      t.setRows(d.stacks);
      for (const s of d.stacks) if (S.open.has(keyOf(s)) && !S.detail.has(keyOf(s))) loadDetail(s);
    },
    every: 60000, poll: true, age,
  });
  envSel.addEventListener("change", () => { S.env = envSel.value; writeUrl(); view.refresh(); });
  instSel.addEventListener("change", () => { S.instance = instSel.value; S.open.clear(); S.detail.clear(); writeUrl(); view.refresh(); });
  function writeUrl() {
    const p = new URLSearchParams(location.search);
    S.instance ? p.set("instance", S.instance) : p.delete("instance");
    S.env ? p.set("env", S.env) : p.delete("env");
    history.replaceState(null, "", location.pathname + (p.toString() ? "?" + p : ""));
  }
  api.get("/api/portainer/instances", { suppressToast: true }).then((list) => {
    S.instances = list;
    if (list.length < 2) return;
    const pick = list.some((i) => i.id === S.instance) ? S.instance : (list.find((i) => i.default) || list[0]).id;
    fill(instSel, list.map((i) => el("option", { value: i.id, selected: i.id === pick }, "Portainer: " + i.name + (i.configured ? "" : " (not set up)"))));
    instSel.hidden = false;
    if (pick !== S.instance) { S.instance = pick; view.refresh(); }
  }, () => {});
  void clear;
})();
