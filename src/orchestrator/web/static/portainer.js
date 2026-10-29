/* Containers (templates/portainer.html), to the Containers wireframe: every stack in Portainer as a
 * table (filter, chips, environment, row menu, Start / Stop / Restart for a selection). A row opens
 * the stack page (stack.js), where its containers, logs, environment and settings are managed;
 * containers outside any stack are one "(no stack)" row per environment. + New stack in a drawer.
 * Everything is read live from Portainer through /api/portainer/*; nothing from Portainer is ever
 * put in as HTML. */
(function () {
  "use strict";
  const { $, el, clear, fill, api, fmt, region, table, drawer, confirm } = window.UI;
  const toast = (m, k) => window.toast(m, k);
  const enc = encodeURIComponent;
  const params = new URLSearchParams(location.search);
  const S = { instance: params.get("instance") || "", env: params.get("env") || "", instances: [] };
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
  const pageOf = (s) => "/portainer/" + s.endpoint_id + "/" + (s.loose ? "-" : enc(s.name)) + (S.instance ? "?instance=" + enc(S.instance) : "");
  const dots = (s) => el("span", { class: "dots", "aria-hidden": "true" }, (s.container_states || []).map((c) => el("i", { class: c.state === "running" ? (c.health === "unhealthy" ? "warn" : "ok") : c.state === "exited" ? "" : "fail", title: c.name + ": " + c.state })));

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
    setTimeout(() => view.refresh(), 800);
  }
  async function redeploy(s) {
    if (!await confirm({ title: "Redeploy " + s.name + "?", body: "Portainer pulls " + gitText(s.git) + " and its images again and recreates the containers. A linked sub-service's deploy (with its checks) is on the stack page.", action: "Redeploy", danger: false })) return;
    try { const r = await api.post(stackUrl(s, "/redeploy"), undefined, { suppressToast: true }); toast(r.message, "ok"); } catch (e) { toast(e.message, "error"); }
    setTimeout(() => view.refresh(), 1200);
  }
  async function deleteStack(s) {
    const linked = s.linked_services || [];
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
    view.refresh();
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
    search: (s) => [s.name, sourceText(s), (s.linked_services || []).join(" "), (s.container_states || []).map((c) => c.name).join(" ")].join(" "),
    sort: { key: "name", dir: 1 },
    toolbarExtra: el("span", { style: { display: "inline-flex", gap: "8px" } }, instSel, envSel),
    age,
    columns: [
      { key: "name", label: "Stack", sort: (s) => (s.loose ? "~" : s.name.toLowerCase()),
        render: (s) => [el("a", { href: pageOf(s), style: { fontWeight: "600", color: "var(--text)" } }, s.name),
          el("span", { class: "sub" }, (S.envs && S.envs[s.endpoint_id]) || "environment " + s.endpoint_id, s.loose ? [" · ", el("span", { class: "tag" }, "loose containers")] : !s.managed ? [" · ", el("span", { class: "tag" }, "not in Portainer's stack list")] : null)] },
      { key: "state", label: "State", sort: (s) => stateOf(s).label, render: (s) => { const x = stateOf(s); return el("span", { class: "pill " + x.cls }, x.label); } },
      { key: "containers", label: "Containers", sort: (s) => s.containers_running || 0, render: (s) => [dots(s), el("span", { class: "faint" }, (s.containers_running || 0) + " / " + (s.containers_total || 0))] },
      { key: "source", label: "Source", phone: false, render: (s) => el("span", { class: "mono", style: { fontSize: "12px" }, title: s.git ? s.git.url : null }, sourceText(s)) },
      { key: "updated", label: "Updated", sort: (s) => s.updated_at || "", render: (s) => s.updated_at ? el("span", { class: "faint", title: fmt.time(s.updated_at) }, fmt.ago(s.updated_at)) : "—" },
      { key: "service", label: "Sub-service", render: (s) => (s.linked_services || []).length ? linkedTo(s.linked_services) : s.loose ? el("span", { class: "faint" }, "—") : [el("span", { class: "faint" }, "not linked"), " · ", el("a", { href: "/sources#portainer" }, "Adopt…")] },
      { key: "open", label: "", phone: false, render: (s) => el("a", { class: "btn ghost small", href: pageOf(s) }, "Open →") },
    ],
    chips: [
      { key: "running", label: "Running", test: (s) => stateOf(s).label === "running" },
      { key: "stopped", label: "Stopped", test: (s) => stateOf(s).label === "stopped" },
      { key: "partly", label: "Partly running", test: (s) => stateOf(s).label === "partly running" },
      { key: "unlinked", label: "Not linked", test: (s) => !s.loose && !(s.linked_services || []).length },
    ],
    menu: (s) => s.loose ? [{ label: "Open", href: pageOf(s) }] : [
      { label: "Open", href: pageOf(s) },
      { label: "Start", onClick: () => stackAction([s], "start") },
      { label: "Stop", onClick: () => stackAction([s], "stop") },
      { label: "Restart", onClick: () => stackAction([s], "restart") },
      s.git ? { label: "Redeploy", onClick: () => redeploy(s) } : null,
      (s.linked_services || []).length ? { label: "Open " + s.linked_services[0], href: "/services/" + enc(s.linked_services[0]) } : { label: "Adopt as a sub-service…", href: "/sources#portainer" },
      s.open_url ? { label: "Open in Portainer ↗", href: s.open_url } : null,
      "-",
      { label: "Delete…", danger: true, onClick: () => deleteStack(s) },
    ].filter(Boolean),
    bulk: (rows) => {
      const real = rows.filter((r) => !r.loose);
      return [
        { label: "Start", onClick: () => stackAction(real, "start") },
        { label: "Stop", onClick: () => stackAction(real, "stop") },
        { label: "Restart", onClick: () => stackAction(real, "restart") },
      ];
    },
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
    const loose = (r.loose || []).map((x) => Object.assign({ name: "(no stack)", loose: true, managed: false, status: "active", git: null }, x));
    return { stacks: r.stacks.concat(loose), banners };
  }
  const view = region(el("div"), { // the table is the visible part; the section only loads
    load,
    render: (d) => {
      fill($("#banners"), d.banners);
      t.setRows(d.stacks);
    },
    every: 60000, poll: true, age,
  });
  envSel.addEventListener("change", () => { S.env = envSel.value; writeUrl(); view.refresh(); });
  instSel.addEventListener("change", () => { S.instance = instSel.value; writeUrl(); view.refresh(); });
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
