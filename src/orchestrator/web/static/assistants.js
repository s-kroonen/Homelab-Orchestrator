/* AI assistants (templates/assistants.html), to the Assistants wireframe: what waits for you, the
 * connections (role, ask first, revoke in the row menu), how to connect, the roles, recent calls
 * (all calls with filters on demand), + New token in a drawer.
 * Data: embedded at first paint; /api/assistants/* again when the "mcp" live topic fires. */
(function () {
  "use strict";
  const { $, el, clear, fill, api, fmt, region, table, drawer, confirm } = window.UI;
  const V = JSON.parse($("#initial").textContent);
  const O = V.overview;
  const toast = (m, k) => window.toast(m, k);
  const STATUS = { ok: "ok", approved: "pending", waiting: "pending", pending: "pending", error: "fail", refused: "fail", denied: "fail", expired: "off", revoked: "off", active: "ok", unused: "off" };
  const copy = (text, what) => (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject())
    .then(() => toast((what || "Copied") + "", "ok"), () => window.prompt("Copy it", text));

  // --- connect, roles ---------------------------------------------------------------------------
  $("#mcp-off").hidden = O.enabled;
  $("#mcp-url").value = O.mcp_url;
  $("#cc-cmd").textContent = "claude mcp add --transport http homelab " + O.mcp_url;
  $("#copy-url").addEventListener("click", () => copy(O.mcp_url, "Address copied"));
  fill($("#roles"), Object.entries(O.roles).reverse().map(([name, r]) => el("div", {}, el("span", { class: "tag" }, name), " ", r.blurb)));
  $("#every-tool").addEventListener("click", () => drawer({
    title: "Roles, permissions and tools",
    sub: "What each role may do, and every tool an assistant can call.",
    saveLabel: "Done",
    async onSave() { /* nothing to save: this drawer only explains */ },
    fields: [
      { type: "custom", render: () => el("div", {},
        el("h3", { class: "ai-h" }, "Permissions"),
        el("dl", { class: "facts" }, Object.entries(O.scopes).map(([n, t]) => [el("dt", {}, el("span", { class: "tag" }, n)), el("dd", {}, t)])),
        el("h3", { class: "ai-h" }, "Roles"),
        el("dl", { class: "facts" }, Object.entries(O.roles).map(([n, r]) => [el("dt", {}, n), el("dd", {}, r.scopes.map((s) => el("span", { class: "tag", style: { marginRight: "4px" } }, s)), el("div", { class: "faint" }, r.blurb))])),
        el("h3", { class: "ai-h" }, "Tools (" + O.tools.length + ")"),
        el("div", { class: "ai-tools" }, O.tools.map((t) => el("div", {},
          el("div", {}, el("strong", { class: "mono" }, t.name), " ", el("span", { class: "tag" }, t.scope), " ",
            el("span", { class: "pill " + (t.risk.startsWith("dangerous") ? "fail" : t.risk.startsWith("act") ? "warn" : "off") }, t.risk)),
          el("div", { class: "faint" }, t.description))))) },
    ],
  }));

  // --- waiting for you ----------------------------------------------------------------------------
  async function decide(r, how) {
    if (how === "deny" && !await confirm({ title: "Deny " + (r.title || r.summary) + "?", body: "The assistant is told it was refused. Nothing is changed.", action: "Deny", danger: false })) return;
    try { await api.post("/api/assistants/requests/" + encodeURIComponent(r.request_id) + "/" + how, {}, { suppressToast: true }); toast(how === "deny" ? "Denied" : "Approved", "ok"); }
    catch (e) { toast(e.message, "error"); }
    refreshAll();
  }
  const waiting = region($("#waiting"), {
    initial: V.requests,
    load: () => api.get("/api/assistants/requests", { suppressToast: true }),
    render: (rows, box) => {
      const open = rows.filter((r) => r.open);
      $("#waiting-card").hidden = !open.length;
      $("#waiting-count").textContent = String(open.length);
      clear(box);
      for (const r of open) {
        box.append(el("div", { class: "ai-req" },
          el("span", { class: "pill warn" }, "needs you"),
          el("div", { style: { minWidth: 0, flex: "1 1 320px" } }, el("strong", {}, r.title || r.summary),
            el("div", { class: "faint", style: { fontSize: "12.5px" } }, [r.title && r.summary !== r.title ? r.summary : "", "asked by connection \"" + r.connection + "\"", fmt.ago(r.requested_at), "expires in " + fmt.ago(r.expires_at).replace(/ from now$/, "")].filter(Boolean).join(" · "))),
          el("button", { type: "button", class: "secondary", onclick: () => decide(r, "deny") }, "Deny"),
          el("a", { class: "btn", href: "/assistants/requests/" + encodeURIComponent(r.request_id) }, "Review and approve…")));
      }
    },
  });

  // --- connections ------------------------------------------------------------------------------------
  function roleDrawer(g) {
    const scopes = Object.keys(O.scopes);
    drawer({
      title: "Change what " + g.label + " may do",
      sub: g.kind === "personal" ? "A personal token" : "Signed in with OAuth" + (g.client ? " as " + g.client : ""),
      values: { role: O.roles[g.role] ? g.role : "custom", scopes: g.scopes.slice(), confirm_writes: g.confirm_writes },
      saveLabel: "Save",
      fields: [
        { key: "role", label: "Role", type: "select", options: Object.entries(O.roles).map(([n, r]) => ({ value: n, label: n + ": " + r.blurb })).concat([{ value: "custom", label: "custom: the permissions below" }]) },
        { key: "scopes", label: "Permissions", type: "checklist", show: (v) => v.role === "custom", options: scopes.map((s) => ({ value: s, label: s, note: O.scopes[s] })) },
        { key: "confirm_writes", label: "Ask before every change, not only the dangerous ones", type: "checkbox" },
      ],
      async onSave(v) {
        const body = { confirm_writes: !!v.confirm_writes };
        if (v.role === "custom") body.scopes = v.scopes; else body.role = v.role;
        await api.patch("/api/assistants/grants/" + g.id, body, { suppressToast: true });
        toast("Saved " + g.label, "ok"); grants.refresh();
      },
    });
  }
  async function setConfirm(g, on) {
    try { await api.patch("/api/assistants/grants/" + g.id, { confirm_writes: on }, { suppressToast: true }); } catch (e) { toast(e.message, "error"); }
    grants.refresh();
  }
  async function revoke(g) {
    if (!await confirm({ title: "Revoke " + g.label + "?", body: "It stops working at once. The assistant has to sign in again (or get a new token) to come back.", typeName: g.label, action: "Revoke" })) return;
    try { await api.post("/api/assistants/grants/" + g.id + "/revoke", {}); } catch (_e) { return; }
    grants.refresh();
  }
  const gt = table($("#grants"), {
    rows: V.grants, key: (g) => g.id, plain: true, urlState: false, noun: ["connection", "connections"],
    columns: [
      { key: "name", label: "Name", render: (g) => [el("strong", {}, g.label),
        el("span", { class: "sub" }, [g.client && g.client !== g.label ? g.client : g.kind === "personal" ? "" : "Claude", g.kind === "personal" ? "personal token" + (g.expires_at && g.state === "active" ? " · expires in " + fmt.ago(g.expires_at).replace(/ from now$/, "") : "") : "signed in with OAuth", "by " + g.admin].filter(Boolean).join(" · "))] },
      { key: "role", label: "Role", render: (g) => el("span", { class: "tag", title: g.scopes.join(", ") }, g.role) },
      { key: "ask", label: "Asks first", phone: false, render: (g) => g.confirm_writes ? "every change" : "dangerous only" },
      { key: "used", label: "Last used", render: (g) => g.last_used_at ? el("span", { title: fmt.time(g.last_used_at) }, fmt.ago(g.last_used_at)) : el("span", { class: "faint" }, "never") },
      { key: "state", label: "State", render: (g) => { const st = g.state === "active" && !g.last_used_at ? "unused" : g.state; return el("span", { class: "pill " + (STATUS[st] || "off") }, st); } },
    ],
    menu: (g) => g.state !== "active" ? [{ label: "Its calls", onClick: () => showCalls(g.id) }] : [
      { label: "Change role…", onClick: () => roleDrawer(g) },
      { label: g.confirm_writes ? "Ask only for dangerous actions" : "Ask before every change", onClick: () => setConfirm(g, !g.confirm_writes) },
      { label: "Its calls", onClick: () => showCalls(g.id) },
      "-",
      { label: "Revoke…", danger: true, onClick: () => revoke(g) },
    ],
    empty: { title: "No connections yet", text: "Add the address on the right to Claude, or make a personal token." },
  });
  const grants = region($("#grants"), {
    initial: V.grants,
    load: () => api.get("/api/assistants/grants", { suppressToast: true }),
    render: (rows) => {
      gt.setRows(rows);
      const sel = $("#f-conn"); const keep = sel.value;
      fill(sel, el("option", { value: "" }, "any connection"), rows.map((g) => el("option", { value: String(g.id) }, g.label)));
      sel.value = keep;
    },
  });

  $("#new-token").addEventListener("click", () => drawer({
    title: "New personal token",
    sub: "For a client that cannot sign in with OAuth (a script, the MCP inspector). It is shown once.",
    values: { label: "", role: "viewer", confirm_writes: false, expires: "" },
    saveLabel: "Create token",
    fields: [
      { key: "label", label: "Name", required: true, placeholder: "inspector" },
      { key: "role", label: "Role", type: "select", options: Object.entries(O.roles).map(([n, r]) => ({ value: n, label: n + ": " + r.blurb })) },
      { key: "confirm_writes", label: "Ask before every change", type: "checkbox" },
      { key: "expires", label: "Expires", type: "select", options: [{ value: "", label: "never" }, { value: "7", label: "in 7 days" }, { value: "30", label: "in 30 days" }, { value: "90", label: "in 90 days" }, { value: "365", label: "in a year" }] },
    ],
    async onSave(v) {
      const made = await api.post("/api/assistants/grants", { label: v.label.trim(), role: v.role, confirm_writes: !!v.confirm_writes, expires_days: v.expires ? Number(v.expires) : null }, { suppressToast: true });
      grants.refresh();
      const box = el("input", { readonly: true, value: made.token, class: "mono", style: { width: "100%" }, "aria-label": "Token" });
      setTimeout(() => {
        confirm({ title: "Copy the token now", body: el("div", {}, el("p", {}, "It is not shown again. " + made.note), box), action: "Copy token", cancel: "Close", danger: false })
          .then((ok) => { if (ok) copy(made.token, "Token copied"); });
        box.select();
      }, 0);
    },
  }));

  // --- calls ---------------------------------------------------------------------------------------------
  let all = false;
  function callRow(c) {
    const args = c.args && Object.keys(c.args).length ? JSON.stringify(c.args, null, 1) : "";
    return el("div", { class: "event-row" },
      el("time", { datetime: c.at, class: "mono faint", title: fmt.time(c.at), style: { fontSize: "12px", whiteSpace: "nowrap" } }, fmt.short(c.at)),
      el("span", { class: "pill " + (STATUS[c.status] || "off") }, c.status),
      el("span", { class: "event-text" }, c.label + " · " + (c.summary || c.tool), el("span", { class: "faint" }, " · " + c.tool + (c.risk !== "read" ? " · " + c.risk : "") + (c.decided_by ? " · decided by " + c.decided_by : "")),
        args ? el("details", { class: "ai-args" }, el("summary", {}, "arguments"), el("pre", { class: "mono" }, args)) : null),
      c.approval_token ? el("a", { href: "/assistants/requests/" + encodeURIComponent(c.approval_token), class: "faint", style: { fontSize: "12px" } }, "request") : null);
  }
  function callQuery() {
    const q = new URLSearchParams({ limit: all ? "100" : "8" });
    if (all) {
      if ($("#f-conn").value) q.set("grant_id", $("#f-conn").value);
      if ($("#f-status").value) q.set("status", $("#f-status").value);
      if ($("#f-risk").value) q.set("risk", $("#f-risk").value);
      if ($("#f-tool").value.trim()) q.set("tool", $("#f-tool").value.trim());
    }
    return q;
  }
  const calls = region($("#calls"), {
    initial: V.calls,
    load: () => api.get("/api/assistants/history?" + callQuery(), { suppressToast: true }),
    render: (rows, box) => {
      clear(box);
      box.append(el("div", { style: { padding: "0 18px 6px" } }, rows.length ? rows.map(callRow) : el("p", { class: "muted", style: { margin: "0 0 8px" } }, all ? "No calls match." : "No calls yet.")));
    },
    age: $("#calls-age"),
  });
  function showAll() {
    all = true; $("#call-filters").hidden = false; $("#all-calls").parentElement.hidden = true; calls.refresh();
  }
  function showCalls(id) { $("#f-conn").value = String(id); showAll(); $("#h-calls").scrollIntoView({ behavior: "smooth" }); }
  $("#all-calls").addEventListener("click", showAll);
  for (const id of ["#f-conn", "#f-status", "#f-risk"]) $(id).addEventListener("change", () => calls.refresh());
  let tt = 0;
  $("#f-tool").addEventListener("input", () => { clearTimeout(tt); tt = setTimeout(() => calls.refresh(), 300); });

  function refreshAll() { waiting.refresh(); grants.refresh(); calls.refresh(); }
  if (window.OrchLive) window.OrchLive.on("mcp", refreshAll, { wait: 700 });
})();
