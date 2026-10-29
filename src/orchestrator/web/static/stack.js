/* One stack (templates/stack.html), to the Stack and StackEnv wireframes: its containers (click
 * one for its details and live logs; restart, stop, kill, recreate, inspect; its check), every
 * container's logs in one view, the stack's environment (secrets hidden until shown; editing
 * redeploys), its Portainer settings and its history. Everything about the stack is read live
 * from Portainer (/api/portainer/*); what the orchestrator keeps about it comes from
 * /api/stacks/<env>/<name>/link. Nothing from Portainer is ever put in as HTML. */
(function () {
  "use strict";
  const { $, el, clear, fill, api, fmt, region, drawer, confirm, menuButton, tabs } = window.UI;
  const O = window.Objects;
  const toast = (m, k) => window.toast(m, k);
  const enc = encodeURIComponent;
  const I = JSON.parse($("#initial").textContent);
  const q = I.instance ? "?instance=" + enc(I.instance) : "";
  const withQ = (path) => path + (I.instance ? (path.includes("?") ? "&" : "?") + "instance=" + enc(I.instance) : "");
  const P = "/api/portainer/endpoints/" + I.endpoint;
  const stackApi = "/api/stacks/" + I.endpoint + "/" + enc(I.name);
  // "(no stack)": the containers of this environment outside any stack - no stack actions,
  // environment or settings, only the containers.
  const LOOSE = !I.name;
  if (LOOSE) for (const t of ["environment", "settings", "history"]) $("#stk-tabs").querySelector('[data-tab="' + t + '"]').hidden = true;

  let D = null;        // the stack and its containers, live
  let L = null;        // what the orchestrator keeps about it (link)
  let envName = "";
  const stats = new Map();
  const details = new Map();
  let openRow = null;

  let T = null; // the tabs, set up last (their first change runs at once)
  let tab = "";

  // --- what it is ------------------------------------------------------------------------------------
  const running = () => (D ? D.containers.filter((c) => c.state === "running").length : 0);
  function stateOf() {
    if (!D) return { cls: "off", label: "…" };
    const n = D.containers.length, r = running();
    if (n && r === n) return { cls: "ok", label: "running · " + r + " of " + n };
    if (r) return { cls: "warn", label: "partly running · " + r + " of " + n };
    return { cls: "off", label: n ? "stopped" : "no containers" };
  }
  const gitText = (g) => (g ? "git " + ((g.reference || "").replace("refs/heads/", "") || "?") + (g.hash ? " @ " + g.hash.slice(0, 7) : "") : "");
  const cpill = (st) => el("span", { class: "pill " + (st === "running" ? "ok" : st === "restarting" || st === "paused" ? "warn" : st === "dead" ? "fail" : "off") }, st);
  const ports = (c) => c.ports.filter((p) => p.public).map((p) => (p.ip && p.ip !== "0.0.0.0" ? p.ip + ":" : "") + p.public + "→" + p.private + (p.proto !== "tcp" ? "/" + p.proto : "")).join(", ");
  const checkOf = (c) => (L ? L.checks.find((k) => k.container === c.name) : null);
  const CHECK_CLS = { healthy: "ok", failed: "fail", down: "off", unknown: "off", skipped: "off", "not run": "off" };

  function drawHeader() {
    const st = stateOf();
    const pill = $("#stk-state"); pill.className = "pill " + st.cls; pill.textContent = st.label;
    if (!D) return;
    if (LOOSE) {
      fill($("#stk-sub"), "Containers on Portainer environment " + (envName || I.endpoint) + " that are in no stack.");
      clear($("#stk-actions")).append(el("a", { class: "btn secondary", href: "/portainer" }, "All stacks"));
      return;
    }
    fill($("#stk-sub"), "Portainer environment " + (envName || I.endpoint),
      L && L.parent ? [" on ", el("a", { href: "/services/" + enc(L.parent.slug) }, L.parent.name)] : "",
      D.git ? " · " + gitText(D.git) : D.managed ? " · " + (D.kind || "compose") : " · not created through Portainer",
      L ? [" · sub-service ", el("a", { href: "/services/" + enc(L.slug) }, L.name)] : [" · ", el("span", { class: "faint" }, "not linked to a service")]);
    const box = clear($("#stk-actions"));
    const up = running() > 0;
    if (D.git) box.append(el("button", { type: "button", class: "secondary", onclick: redeploy }, "Redeploy"));
    if (up) box.append(el("button", { type: "button", class: "secondary", onclick: () => stackAction("restart") }, "Restart"),
      el("button", { type: "button", class: "secondary", onclick: () => stackAction("stop") }, "Stop"));
    else box.append(el("button", { type: "button", onclick: () => stackAction("start") }, "Start"));
    box.append(menuButton(() => [
      up ? { label: "Start", onClick: () => stackAction("start") } : null,
      D.git ? { label: "Pull images and redeploy", onClick: redeploy } : null,
      D.open_url ? { label: "Open in Portainer ↗", href: D.open_url } : null,
      L ? { label: "Open the sub-service", href: "/services/" + enc(L.slug) } : { label: "Adopt as a sub-service…", href: "/sources#portainer" },
      "-",
      { label: "Delete stack…", danger: true, onClick: deleteStack },
    ].filter(Boolean), "More actions"));
  }

  // --- stack actions ------------------------------------------------------------------------------------
  async function stackAction(verb) {
    if (verb !== "start" && !await confirm({ title: (verb === "stop" ? "Stop " : "Restart ") + I.name + "?",
      body: verb === "stop" ? "Its containers stop; whatever uses them stops answering." : "Its containers restart; anyone using them is cut off for a moment.",
      action: verb === "stop" ? "Stop" : "Restart", danger: verb === "stop" })) return;
    try { const r = await api.post(withQ(stackApi + "/" + verb), undefined, { suppressToast: true }); toast(r.message || verb + " sent", "ok"); }
    catch (e) { toast(e.message, "error"); }
    setTimeout(refreshAll, 800);
  }
  async function redeploy() {
    const guarded = L && L.deploy.enabled;
    if (!await confirm({ title: "Redeploy " + I.name + "?",
      body: guarded ? "Runs its deploy: the checks before, a pull of " + gitText(D.git) + " and a redeploy, then the checks after (the sub-service's deploy settings)."
        : "Portainer pulls " + gitText(D.git) + " and its images again and recreates the containers.",
      action: "Redeploy", danger: false })) return;
    try {
      if (guarded) { await api.post("/api/services/" + enc(L.slug) + "/deploy", { guards: true }, { suppressToast: true }); toast("Deploy started: follow it in History", "ok"); }
      else { const r = await api.post(withQ(stackApi + "/redeploy"), undefined, { suppressToast: true }); toast(r.message, "ok"); }
    } catch (e) { toast(e.message, "error"); }
    setTimeout(refreshAll, 1500);
  }
  async function deleteStack() {
    const linked = L ? [L.slug].concat(L.others) : [];
    const down = el("input", { type: "checkbox" }); const keep = el("input", { type: "checkbox" });
    const ok = await confirm({
      title: "Delete stack " + I.name + "?",
      body: el("div", {}, el("p", {}, "Deletes it in Portainer (its volumes are kept) and then the link here. Its containers stop."),
        el("p", {}, linked.length ? "Linked here as " + linked.join(", ") + ": removed with it unless you keep it." : "Nothing here is linked to it."),
        el("label", { class: "inline" }, down, " switch linked services off first, in the right order"), el("br"),
        linked.length ? el("label", { class: "inline" }, keep, " keep the sub-service (only clear the link)") : null),
      typeName: I.name, action: "Delete the stack",
    });
    if (!ok) return;
    try {
      const r = await window.apiFetch("DELETE", withQ(stackApi), { confirm_name: I.name, take_down: down.checked, remove_services: !keep.checked }, { suppressToast: true });
      toast("Deleted " + r.stack + " (volumes kept)." + (r.warnings.length ? " " + r.warnings.join(" ") : ""), "ok");
      location.href = "/portainer";
    } catch (e) { toast(e.message, "error"); }
  }
  async function containerAction(c, verb) {
    const words = { stop: "Stop", restart: "Restart", kill: "Kill", recreate: "Recreate" };
    const why = { stop: "It stops; whatever uses it stops answering.", restart: "Anyone using it is cut off for a moment.", kill: "It is stopped at once, without a chance to shut down cleanly.", recreate: "Its image is pulled again and the container is created anew with the same settings." };
    if (verb !== "start" && !await confirm({ title: words[verb] + " " + c.name + "?", body: why[verb], action: words[verb], danger: verb === "stop" || verb === "kill" })) return;
    try { const r = await api.post(withQ("/api/containers/" + I.endpoint + "/" + enc(c.name) + "/" + verb), undefined, { suppressToast: true }); toast(r.message || verb + " sent", "ok"); }
    catch (e) { toast(e.message, "error"); }
    details.delete(c.name);
    setTimeout(refreshAll, 800);
  }

  // --- its check (a probe on the linked sub-service) ------------------------------------------------------
  async function setCheck(k, body, what) {
    try { await api.put("/api/registry/probes/" + k.id, body, { suppressToast: true }); toast(what, "ok"); } catch (e) { toast(e.message, "error"); }
    link.refresh();
  }
  async function addCheck(c) {
    const extra = I.instance ? { instance: I.instance } : {};
    try {
      await api.post("/api/registry/services/" + enc(L.slug) + "/probes", { name: "container:" + c.name, kind: "portainer", required: true, config: Object.assign({ endpoint: I.endpoint, container: c.name }, extra) }, { suppressToast: true });
      toast("Checking " + c.name + " now", "ok");
    } catch (e) { toast(e.message, "error"); }
    link.refresh();
  }
  function checkCell(c) {
    const k = checkOf(c);
    if (!L) return el("span", { class: "faint" }, "not linked");
    if (!k) return el("span", { class: "faint" }, "no check");
    const bits = [!k.enabled ? "off" : k.required ? "required" : "optional", k.backup_waits ? "backup waits" : ""].filter(Boolean).join(" · ");
    return [el("span", { class: "pill " + (CHECK_CLS[k.state] || "off"), title: k.message || null }, k.state), " ", el("span", { class: "faint", style: { fontSize: "12px" } }, "· " + bits)];
  }
  function checkMenu(c) {
    const k = checkOf(c);
    if (!L) return [];
    if (!k) return [{ heading: "Its check" }, { label: "Check this container", onClick: () => addCheck(c) }];
    return [{ heading: "Its check" },
      k.required ? { label: "Make it optional", onClick: () => setCheck(k, { required: false }, c.name + ": optional now") } : { label: "Make it required", onClick: () => setCheck(k, { required: true }, c.name + ": required now") },
      k.enabled ? { label: "Switch the check off", onClick: () => setCheck(k, { enabled: false }, c.name + ": not checked now") } : { label: "Switch the check on", onClick: () => setCheck(k, { enabled: true }, c.name + ": checked again") },
      k.backup_waits ? { label: "A backup does not wait for it", onClick: () => setCheck(k, { backup_waits: false }, "A backup no longer waits for " + c.name) }
        : { label: "A backup waits for it", onClick: () => setCheck(k, { backup_waits: true }, "A backup of " + (L.parent ? L.parent.name : "its parent") + " now waits for " + c.name) }];
  }

  // --- containers ----------------------------------------------------------------------------------------
  function toggle(c) {
    if (openRow === c.name) { openRow = null; rowLogs.stop(); } else { openRow = c.name; loadDetail(c); }
    drawContainers();
  }
  async function loadDetail(c) {
    if (!details.has(c.name)) {
      try { details.set(c.name, await api.get(withQ(P + "/containers/" + enc(c.name)), { suppressToast: true })); } catch (e) { details.set(c.name, { error: e.message }); }
      drawContainers();
    }
  }
  function detailPanel(c) {
    const d = details.get(c.name);
    const k = checkOf(c);
    const info = !d ? el("p", { class: "faint" }, "Loading…") : d.error ? el("p", {}, el("span", { class: "pill fail" }, "error"), " " + d.error) : el("dl", { class: "facts" },
      [["Image", [el("span", { class: "mono" }, c.image), el("span", { class: "faint" }, " · " + (c.image_id || "").replace("sha256:", "").slice(0, 12))]],
        ["Started", d.started_at ? fmt.time(d.started_at) + " · restart policy " + (d.restart_policy || "none") + (d.restart_count ? " · restarted " + d.restart_count + "×" : "") : "—"],
        d.exit_code != null && c.state !== "running" ? ["Exit code", String(d.exit_code)] : null,
        ["Volumes", d.mounts.length ? d.mounts.map((m) => el("div", { class: "mono", style: { fontSize: "12.5px" } }, m.source + " → " + m.destination + (m.read_only ? " (read-only)" : ""))) : "—"],
        ["Networks", d.networks.length ? el("span", { class: "mono" }, d.networks.join(", ")) : "—"],
        ["Environment", d.env_names.length ? [d.env_names.length + " variable" + (d.env_names.length === 1 ? "" : "s") + " (names only): ", el("span", { class: "mono", style: { fontSize: "12px" } }, d.env_names.join(", "))] : "—"],
        ["Health", [c.health ? ["Docker healthcheck ", el("span", { class: "pill " + (c.health === "healthy" ? "ok" : c.health === "unhealthy" ? "fail" : "off") }, c.health)] : "no Docker healthcheck",
          k ? [" · check ", el("span", { class: "mono" }, k.name), " " + (k.required ? "required" : "optional") + (k.backup_waits ? ", backup waits" : "")] : ""]],
      ].filter(Boolean).map(([a, b]) => [el("dt", {}, a), el("dd", {}, b)]));
    return el("div", { class: "stk-detail" }, el("div", { class: "stk-info" }, info), el("div", { class: "stk-logs" }, rowLogs.mount(c)));
  }
  function drawContainers() {
    const box = clear($("#containers"));
    $("#n-containers").textContent = D ? String(D.containers.length) : "";
    $("#ct-intro").textContent = L ? "Each one is checked on the sub-service " + L.name + "; \"backup waits\" means a backup of " + (L.parent ? L.parent.name : "its parent") + " waits for it." : "Not linked to a service here, so nothing checks these.";
    if (!D) { box.append(el("div", { class: "skeleton", style: { padding: "14px 18px" } }, el("i"), el("i"))); return; }
    if (!D.containers.length) { box.append(el("div", { class: "dt-empty" }, el("strong", {}, "No containers"), el("div", {}, "The stack is stopped or has none."))); return; }
    const rows = [];
    for (const c of D.containers) {
      const st = stats.get(c.name);
      const open = openRow === c.name;
      rows.push(el("tr", { class: open ? "expanded" : null },
        el("td", { class: "first" }, el("button", { type: "button", class: "ct-toggle", "aria-expanded": String(open), onclick: () => toggle(c) }, el("span", { class: "tw", "aria-hidden": "true" }, open ? "▾" : "▸"), c.name),
          c.service && c.service !== c.name ? el("span", { class: "sub" }, c.service) : null),
        el("td", { class: "mono faint", "data-label": "Image", style: { fontSize: "12px" }, title: c.image }, c.image),
        el("td", { "data-label": "State" }, cpill(c.state)),
        el("td", { class: "faint", "data-label": "Up", style: { whiteSpace: "nowrap" } }, (c.status_text || "").replace(/^Up /, "").replace(/ \((healthy|unhealthy|health: starting)\)$/, "")),
        el("td", { class: "mono", "data-label": "Ports", style: { fontSize: "12px" } }, ports(c) || "—"),
        el("td", { class: "faint", "data-label": "CPU · memory", style: { whiteSpace: "nowrap" } }, st ? Math.round(st.cpu_percent) + "% · " + fmt.bytes(st.mem_bytes) : "—"),
        el("td", { "data-label": "Check" }, checkCell(c)),
        el("td", { class: "menu-cell", style: { whiteSpace: "nowrap" } },
          el("button", { type: "button", class: "ghost small", onclick: () => { if (openRow !== c.name) toggle(c); } }, "Logs"),
          el("button", { type: "button", class: "ghost small", onclick: () => containerAction(c, "restart") }, "Restart"),
          menuButton(() => [
            c.state === "running" ? null : { label: "Start", onClick: () => containerAction(c, "start") },
            c.state === "running" ? { label: "Stop…", onClick: () => containerAction(c, "stop") } : null,
            { label: "Restart…", onClick: () => containerAction(c, "restart") },
            c.state === "running" ? { label: "Kill…", danger: true, onClick: () => containerAction(c, "kill") } : null,
            { label: "Recreate (pull image)…", onClick: () => containerAction(c, "recreate") },
            { label: "Inspect", onClick: () => inspect(c) },
            c.open_url ? { label: "Open in Portainer ↗", href: c.open_url } : null,
          ].concat(checkMenu(c)).filter(Boolean), "Actions for " + c.name))));
      if (open) rows.push(el("tr", { class: "detail-row" }, el("td", { colspan: 8 }, detailPanel(c))));
    }
    box.append(el("div", { class: "dt-scroll" }, el("table", { class: "dt" },
      el("thead", {}, el("tr", {}, ["Container", "Image", "State", "Up", "Ports", "CPU · memory", "Check", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows))));
  }
  async function inspect(c) {
    let text = "";
    try { text = JSON.stringify(await api.get(withQ(P + "/containers/" + enc(c.name) + "/inspect"), { suppressToast: true }), null, 2); } catch (e) { toast(e.message, "error"); return; }
    drawer({ title: "Inspect " + c.name, sub: "docker inspect, environment values masked", wide: true, saveLabel: "Done", async onSave() { /* nothing */ },
      fields: [{ type: "custom", render: () => el("pre", { class: "sys-pre", style: { maxHeight: "70vh", overflow: "auto" } }, text) }] });
  }

  // --- logs: one container (in its row) and all of them (Logs tab) ------------------------------------------
  function logView(o) {
    // o.fetch() -> [{ts, text, stream, who}], o.chips: names or null, o.name for the file
    let lines = []; let timer = 0; let target = null; let only = "";
    const out = el("pre", { class: "log", "aria-live": "off", style: { maxHeight: o.height || "360px" } });
    const follow = el("input", { type: "checkbox", checked: true }); const ts = el("input", { type: "checkbox", checked: true });
    const filter = el("input", { type: "search", placeholder: "Search…", "aria-label": "Search the logs" });
    const tail = el("select", { "aria-label": "Lines" }, [100, 200, 500, 1000].map((n) => el("option", { value: String(n), selected: n === (o.tail || 200) }, n + " lines")));
    const chips = el("span", { class: "log-chips" });
    function draw() {
      const f = filter.value.toLowerCase();
      const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 24;
      fill(out, lines.filter((l) => (!only || l.who === only) && (!f || l.text.toLowerCase().includes(f))).map((l) =>
        el("div", { class: l.stream === "stderr" ? "err" : null }, l.ts && ts.checked ? el("span", { class: "ts" }, fmt.time(l.ts).split(" ").pop() + "  ") : null, l.who && o.chips ? el("span", { class: "who" }, l.who + "  ") : null, l.text)));
      if (stick) out.scrollTop = out.scrollHeight;
    }
    async function load() {
      try { lines = await o.fetch(target, Number(tail.value)); draw(); } catch (e) { fill(out, el("div", { class: "err" }, e.message)); }
    }
    function arm() { clearInterval(timer); if (follow.checked) timer = setInterval(load, 3000); }
    follow.addEventListener("change", arm);
    tail.addEventListener("change", load);
    for (const x of [ts, filter]) x.addEventListener("input", draw);
    const download = () => {
      const text = lines.map((l) => (l.ts ? l.ts + " " : "") + (l.who && o.chips ? l.who + " " : "") + l.text).join("\n") + "\n";
      const a = el("a", { href: URL.createObjectURL(new Blob([text], { type: "text/plain" })), download: (o.name(target) || "logs") + ".log" });
      a.click(); URL.revokeObjectURL(a.href);
    };
    const tools = el("div", { class: "log-tools" }, el("label", { class: "inline" }, follow, " follow"), el("label", { class: "inline" }, ts, " timestamps"), tail, filter, chips,
      el("button", { type: "button", class: "ghost small", onclick: download }, "Download"),
      o.more ? el("button", { type: "button", class: "ghost small", onclick: o.more }, "Logs tab →") : null);
    const node = el("div", { class: "log-view" }, tools, out);
    return {
      mount(t) { target = t; load(); arm(); return node; },
      start() { if (o.chips) fill(chips, ["", ...o.chips()].map((n) => el("button", { type: "button", class: "chip small", "aria-pressed": String(only === n), onclick: () => { only = n; this.start(); draw(); } }, n || "All"))); load(); arm(); },
      stop() { clearInterval(timer); },
      node,
    };
  }
  const logsOf = async (name, n) => (await api.get(withQ(P + "/containers/" + enc(name) + "/logs?tail=" + n + "&timestamps=true"), { suppressToast: true })).lines.map((l) => Object.assign(l, { who: name }));
  const rowLogs = logView({ fetch: (c, n) => logsOf(c.name, n), name: (c) => c && c.name, tail: 100, height: "300px", more: () => T.show("logs") });
  const allLogs = logView({
    chips: () => (D ? D.containers.map((c) => c.name) : []), name: () => I.name, height: "65vh",
    fetch: async (_t, n) => {
      const per = await Promise.all((D ? D.containers : []).map((c) => logsOf(c.name, n).catch(() => [])));
      return per.flat().sort((a, b) => String(a.ts || "").localeCompare(String(b.ts || "")));
    },
  });
  $("#all-logs").append(allLogs.node);

  // --- environment ----------------------------------------------------------------------------------------------
  let E = null;
  async function loadEnv() {
    try { E = await api.get(withQ(stackApi + "/env"), { suppressToast: true }); } catch (e) { E = { error: e.message }; }
    drawEnv();
  }
  async function reveal(v) {
    try { const r = await api.post(withQ(stackApi + "/env/reveal"), { name: v.name }, { suppressToast: true }); v.value = r.value; v.shown = true; drawEnv(); }
    catch (e) { toast(e.message, "error"); }
  }
  function drawEnv() {
    const box = clear($("#env"));
    $("#n-env").textContent = E && E.vars ? String(E.vars.length) : D ? String((D.env_names || []).length) : "";
    $("#env-edit").hidden = !(E && E.editable);
    if (!E) { box.append(el("div", { class: "skeleton", style: { padding: "14px 18px" } }, el("i"), el("i"))); return; }
    if (E.error) { box.append(el("p", { style: { padding: "0 18px" } }, el("span", { class: "pill fail" }, "error"), " " + E.error)); return; }
    if (!E.editable) { box.append(el("p", { class: "muted", style: { padding: "0 18px" } }, E.why)); return; }
    if (!E.vars.length) { box.append(el("div", { class: "dt-empty" }, el("strong", {}, "No variables"), el("div", {}, "The compose file gets nothing from Portainer."))); return; }
    box.append(el("div", { class: "dt-scroll" }, el("table", { class: "dt" },
      el("thead", {}, el("tr", {}, ["Name", "Value", "Used by"].map((h) => el("th", {}, h)))),
      el("tbody", {}, E.vars.map((v) => el("tr", {},
        el("td", { class: "first mono" }, v.name),
        el("td", { class: "mono", "data-label": "Value", style: { overflowWrap: "anywhere" } }, v.secret && !v.shown ? ["•••••••• ", el("button", { type: "button", class: "ghost small", onclick: () => reveal(v) }, "show")] : v.value),
        el("td", { class: "faint", "data-label": "Used by" }, v.used_by.length ? v.used_by.join(", ") : "—")))))));
  }
  const HIDDEN = "<hidden, unchanged>";
  $("#env-edit").addEventListener("click", () => {
    const start = E.vars.map((v) => v.name + "=" + (v.secret && !v.shown ? HIDDEN : v.value)).join("\n");
    const changes = el("div", { class: "banner warn", style: { fontSize: "13px" } });
    const parse = (text) => text.split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#")).map((l) => {
      const i = l.indexOf("="); if (i < 1) throw new Error("\"" + l + "\" is not NAME=value");
      const name = l.slice(0, i).trim(); const value = l.slice(i + 1);
      return { name, value: value === HIDDEN ? null : value };
    });
    function describe(text) {
      let want;
      try { want = parse(text); } catch (e) { fill(changes, e.message); return; }
      const before = Object.fromEntries(E.vars.map((v) => [v.name, v]));
      const names = new Set(want.map((w) => w.name));
      const added = want.filter((w) => !before[w.name]).map((w) => w.name);
      const changed = want.filter((w) => before[w.name] && w.value !== null && !(before[w.name].value === w.value && (!before[w.name].secret || before[w.name].shown))).map((w) => w.name);
      const removed = E.vars.filter((v) => !names.has(v.name)).map((v) => v.name);
      const parts = [added.length ? "added " + added.join(", ") : "", changed.length ? "changed " + changed.join(", ") : "", removed.length ? "removed " + removed.join(", ") : ""].filter(Boolean);
      fill(changes, parts.length ? ["Changes: " + parts.join("; ") + ". Saving redeploys " + I.name + " (" + (D ? D.containers.length : "its") + " containers restart). The checks run again after it; Activity records the names, not the values."] : "No changes yet.");
    }
    let current = start;
    const d = drawer({
      title: "Edit environment", sub: I.name + " · saving redeploys the stack", saveLabel: "Save and redeploy",
      values: { text: start },
      fields: [
        { key: "text", label: "Variables, one per line", type: "textarea", rows: Math.max(8, E.vars.length + 3), mono: true, help: "NAME=value. A hidden value stays as it is unless you type a new one in place of " + HIDDEN + "." },
        { type: "custom", render: () => changes },
      ],
      async onSave(v) {
        const want = parse(v.text);
        const r = await api.put(withQ(stackApi + "/env"), { vars: want }, { suppressToast: true });
        toast(r.redeployed ? "Saved; " + I.name + " is redeploying" : "Nothing changed", "ok");
        loadEnv(); setTimeout(refreshAll, 1200);
      },
    });
    const ta = d.el.querySelector("textarea");
    describe(current);
    if (ta) ta.addEventListener("input", () => { current = ta.value; describe(current); });
  });

  // --- settings ----------------------------------------------------------------------------------------------------
  function drawSettings() {
    if (!D) return;
    const g = D.git;
    $("#git-edit").hidden = !g || D.id == null;
    fill($("#pset"), el("dl", { class: "facts" }, [
      ["Source", g ? el("span", { class: "mono", style: { overflowWrap: "anywhere" } }, "git " + g.url + " · " + (g.reference || "").replace("refs/heads/", "") + " · " + (g.path || "docker-compose.yml") + (g.has_credentials ? " (with credentials)" : "")) : D.managed ? "compose text in Portainer (" + (D.kind || "compose") + ")" : "a compose project Portainer did not create"],
      ["Updates", g ? (D.auto_update_interval ? "Portainer checks the repository every " + D.auto_update_interval : D.has_webhook ? "on Portainer's webhook" : "only when redeployed") : "—"],
      ["Environment", (envName || "environment " + I.endpoint) + " · " + D.type],
      ["Created", D.created_at ? fmt.time(D.created_at) + (D.created_by ? " by " + D.created_by : "") : "—"],
      ["Updated", D.updated_at ? fmt.time(D.updated_at) + (D.updated_by ? " by " + D.updated_by : "") : "—"],
    ].map(([a, b]) => [el("dt", {}, a), el("dd", {}, b)])));
    if (!L) { fill($("#oset"), el("p", { class: "muted", style: { margin: 0 } }, "Not linked to a service here, so nothing checks it, backs it up or deploys it. ", el("a", { href: "/sources#portainer" }, "Adopt it as a sub-service →"))); return; }
    const waits = L.checks.filter((k) => k.backup_waits).map((k) => k.container || k.name);
    fill($("#oset"), el("dl", { class: "facts" }, [
      ["Sub-service", [el("a", { href: "/services/" + enc(L.slug) }, L.name), L.parent ? [" under ", el("a", { href: "/services/" + enc(L.parent.slug) }, L.parent.name)] : "", " ", el("span", { class: "pill " + (CHECK_CLS[L.verdict] || "off") }, L.verdict)]],
      ["Deploys", L.deploy.enabled ? [(L.deploy.webhook ? "on a push (webhook)" : "by hand") + ", checks before and after · pull images " + (L.deploy.pull_image ? "on" : "off") + " · prune " + (L.deploy.prune ? "on" : "off") + " · ", el("a", { href: "/services/" + enc(L.slug) + "?tab=related#deploys" }, "Deploy settings")] : ["none · ", el("a", { href: "/services/" + enc(L.slug) + "?tab=related#deploys" }, "Add a deploy method")]],
      ["Backup", L.parent ? "with " + L.parent.name + "'s guest" + (L.backup_gate === "required" ? "; waits for the whole stack" : waits.length ? "; waits for " + waits.join(", ") : "; does not wait for it") : "—"],
      ["Checks", L.checks.length + " on the sub-service: " + L.checks.map((k) => k.name).join(", ")],
    ].map(([a, b]) => [el("dt", {}, a), el("dd", {}, b)])));
  }
  $("#git-edit").addEventListener("click", () => {
    const g = D.git;
    drawer({
      title: "Stack settings in Portainer", sub: I.name + " · from " + g.url,
      values: { reference: g.reference || "refs/heads/main", auto: !!D.auto_update_interval, interval: D.auto_update_interval || "5m" },
      saveLabel: "Save in Portainer",
      fields: [
        { key: "reference", label: "Branch or tag", mono: true, required: true, help: "refs/heads/main, refs/tags/v1.2. The next redeploy uses it." },
        { key: "auto", label: "Let Portainer check the repository by itself", type: "checkbox", help: "Portainer redeploys when the branch moves. A deploy through the orchestrator (with its checks) is usually better." },
        { key: "interval", label: "Every", mono: true, placeholder: "5m", show: (v) => v.auto },
      ],
      async onSave(v) {
        const r = await api.put(withQ(stackApi + "/git"), { reference: v.reference.trim(), auto_update_interval: v.auto ? (v.interval || "5m").trim() : null }, { suppressToast: true });
        toast(r.message, "ok"); refreshAll();
      },
    });
  });

  // --- history --------------------------------------------------------------------------------------------------------
  async function loadHistory() {
    const box = clear($("#history"));
    const asks = [api.get("/api/activity?limit=50&q=" + enc(I.name), { suppressToast: true })];
    if (L) asks.push(api.get("/api/activity?limit=50&service=" + enc(L.slug), { suppressToast: true }));
    $("#hist-all").href = L ? "/activity?service=" + enc(L.slug) : "/activity?q=" + enc(I.name);
    let events = [];
    try { events = (await Promise.all(asks)).flat(); } catch (e) { box.append(el("p", { style: { padding: "0 18px" } }, e.message)); return; }
    const seen = new Set();
    events = events.filter((ev) => { const k = ev.type + ":" + (ev.id || ev.at); if (seen.has(k)) return false; seen.add(k); return true; })
      .sort((a, b) => String(b.at).localeCompare(String(a.at)));
    if (!events.length) { box.append(el("p", { class: "muted", style: { padding: "0 18px 14px", margin: 0 } }, "Nothing yet.")); return; }
    events.slice(0, 50).forEach((ev) => box.append(O.eventRow(ev)));
  }
  const recent = region($("#recent"), {
    load: () => (LOOSE ? Promise.resolve([]) : L ? api.get("/api/activity?limit=5&service=" + enc(L.slug), { suppressToast: true }) : api.get("/api/activity?limit=5&q=" + enc(I.name), { suppressToast: true })),
    render: (list, box) => { clear(box); if (!list.length) box.append(el("p", { class: "muted", style: { margin: 0 } }, "Nothing yet.")); else list.forEach((ev) => box.append(O.eventRow(ev))); },
    topics: ["run", "audit"], wait: 1500, skeletonRows: 2,
  });

  // --- loading ----------------------------------------------------------------------------------------------------------
  const stack = region(el("div"), {
    load: () => (LOOSE
      ? api.get(withQ(P + "/containers"), { suppressToast: true }).then((all) => ({ name: "(no stack)", id: null, managed: false, git: null, type: "containers", env_names: [], containers: all.filter((c) => !c.stack) }))
      : api.get(withQ(P + "/stacks/" + enc(I.name)), { suppressToast: true })),
    render: (d) => {
      const firstLoad = D === null;
      D = d;
      for (const c of d.containers) if (c.state === "running") api.get(withQ(P + "/containers/" + enc(c.name) + "/stats"), { suppressToast: true }).then((st) => { stats.set(c.name, st); drawContainers(); }, () => {});
      fill($("#stk-banners"));
      drawAll();
      if (firstLoad && tab === "logs") allLogs.start(); // opened on the Logs tab: it waited for the containers
    },
    onError: (e) => fill($("#stk-banners"), el("div", { class: "banner fail" }, el("span", { class: "pill fail" }, "error"), " " + e.message, " ", el("a", { class: "btn secondary small", href: "/infra/connections" }, "Check connection"))),
    topics: ["run", "audit"], wait: 1500, every: 30000, poll: true, age: $("#ct-age"),
  });
  const link = region(el("div"), {
    load: () => (LOOSE ? Promise.resolve({ link: null }) : api.get(withQ(stackApi + "/link"), { suppressToast: true })),
    render: (d) => { L = d.link; drawAll(); recent.refresh(); },
    topics: ["health", "registry"], wait: 1500,
  });
  function drawAll() { drawHeader(); drawContainers(); drawSettings(); if (E) drawEnv(); else $("#n-env").textContent = D ? String((D.env_names || []).length) : ""; }
  function refreshAll() { stack.refresh(); link.refresh(); }
  T = tabs($("#stk-tabs"), { onChange: (t) => { tab = t; if (t === "logs") allLogs.start(); else allLogs.stop(); if (t === "environment") loadEnv(); if (t === "history") loadHistory(); } });
  $("#to-history").addEventListener("click", (ev) => { ev.preventDefault(); T.show("history"); window.scrollTo({ top: 0 }); });
  api.get(withQ("/api/portainer/environments"), { suppressToast: true }).then((envs) => { const e = envs.find((x) => x.id === I.endpoint); envName = e ? e.name : ""; drawAll(); }, () => {});
  void q;
})();
