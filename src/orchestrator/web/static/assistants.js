/* AI assistants page: connections, waiting requests, history. Everything comes from
   /api/assistants/*; the page refreshes when the "mcp" live topic fires. */
(() => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, props, ...kids) => {
    const n = document.createElement(tag);
    Object.assign(n, props || {});
    for (const k of kids) n.append(k && k.nodeType ? k : document.createTextNode(k ?? ""));
    return n;
  };
  const when = (iso) => (iso ? fmtTime(iso) : "never");
  const api = (method, url, body) => apiFetch(method, url, body);
  let overview = null;
  let grants = [];

  function chips(scopes) {
    return scopes.map((s) => el("span", { className: "chip" + (s === "propose" ? " propose" : "") }, s));
  }

  function roleOptions(select, selected) {
    select.replaceChildren(
      ...Object.entries(overview.roles).map(([name, r]) =>
        el("option", { value: name, selected: name === selected, title: r.blurb }, name + " — " + r.blurb)),
    );
  }

  async function loadOverview() {
    overview = await api("GET", "/api/assistants/overview");
    $("off").hidden = overview.enabled;
    $("mcp-url").textContent = overview.mcp_url;
    $("cc-cmd").textContent = "claude mcp add --transport http homelab " + overview.mcp_url;
    roleOptions($("pt-role"), "viewer");
    $("roles").replaceChildren(
      el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "Role"), el("th", {}, "Permissions"), el("th", {}, "What it is"))),
        el("tbody", {}, ...Object.entries(overview.roles).map(([n, r]) =>
          el("tr", {}, el("td", {}, n), el("td", {}, ...chips(r.scopes)), el("td", {}, r.blurb))))),
      el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "Permission"), el("th", {}, "Allows"))),
        el("tbody", {}, ...Object.entries(overview.scopes).map(([n, t]) => el("tr", {}, el("td", {}, n), el("td", {}, t))))),
    );
    $("tool-body").replaceChildren(
      ...overview.tools.map((t) => el("tr", {},
        el("td", {}, t.name), el("td", {}, t.scope),
        el("td", { className: "risk-" + (t.risk.startsWith("dangerous") ? "dangerous" : t.risk.startsWith("act") ? "act" : "read") }, t.risk),
        el("td", {}, t.description))),
    );
  }

  async function loadGrants() {
    grants = await api("GET", "/api/assistants/grants");
    const body = $("grant-body");
    if (!grants.length) { body.replaceChildren(el("tr", {}, el("td", { colSpan: 7 }, "No connections yet."))); }
    else body.replaceChildren(...grants.map(grantRow));
    const sel = $("f-conn"), keep = sel.value;
    sel.replaceChildren(el("option", { value: "" }, "any connection"),
      ...grants.map((g) => el("option", { value: g.id }, g.label)));
    sel.value = keep;
  }

  function grantRow(g) {
    const active = g.state === "active";
    const edit = el("button", { className: "secondary small" }, "Edit");
    edit.addEventListener("click", () => editGrant(g));
    const revoke = el("button", { className: "danger small" }, "Revoke");
    revoke.addEventListener("click", async () => {
      if (!confirm("Revoke " + g.label + "? It stops working at once.")) return;
      await api("POST", "/api/assistants/grants/" + g.id + "/revoke");
      refresh();
    });
    return el("tr", {},
      el("td", {}, g.label, g.client && g.client !== g.label ? el("div", { className: "msg" }, g.client) : "",
        g.confirm_writes ? el("div", { className: "msg" }, "asks before every change") : ""),
      el("td", {}, g.kind === "personal" ? "token" : "connector", el("div", { className: "msg" }, "by " + g.admin)),
      el("td", {}, g.role),
      el("td", {}, ...chips(g.scopes)),
      el("td", {}, when(g.last_used_at)),
      el("td", { className: "st-" + g.state }, g.state),
      el("td", {}, ...(active ? [edit, " ", revoke] : [])));
  }

  function editGrant(g) {
    const row = $("grant-body");
    const sel = el("select");
    roleOptions(sel, g.role);
    sel.append(el("option", { value: "custom", selected: g.role === "custom" }, "custom"));
    const boxes = Object.keys(overview.scopes).map((s) => {
      const b = el("input", { type: "checkbox", value: s, checked: g.scopes.includes(s) });
      return [b, el("label", {}, b, " " + s + " ")];
    });
    sel.addEventListener("change", () => {
      const r = overview.roles[sel.value];
      if (r) boxes.forEach(([b]) => { b.checked = r.scopes.includes(b.value); });
    });
    const confirmBox = el("input", { type: "checkbox", checked: g.confirm_writes });
    const save = el("button", {}, "Save");
    const cancel = el("button", { className: "secondary" }, "Cancel");
    cancel.addEventListener("click", loadGrants);
    save.addEventListener("click", async () => {
      const scopes = boxes.filter(([b]) => b.checked).map(([b]) => b.value);
      await api("PATCH", "/api/assistants/grants/" + g.id, { scopes, confirm_writes: confirmBox.checked });
      refresh();
    });
    const tr = [...row.children].find((r) => r.firstChild.firstChild.textContent === g.label);
    tr.replaceChildren(el("td", { colSpan: 7 },
      el("strong", {}, g.label), " ", sel, " ", ...boxes.map(([, l]) => l),
      el("label", {}, confirmBox, " ask before every change "), save, " ", cancel));
  }

  async function loadRequests() {
    const rows = await api("GET", "/api/assistants/requests");
    const open = rows.filter((r) => r.open).length;
    $("waiting-count").textContent = open ? "(" + open + " open)" : "";
    const body = $("req-body");
    if (!rows.length) { body.replaceChildren(el("tr", {}, el("td", { colSpan: 5 }, "Nothing has asked for approval."))); return; }
    body.replaceChildren(...rows.slice(0, 20).map((r) => el("tr", {},
      el("td", {}, when(r.requested_at)), el("td", {}, r.connection), el("td", {}, r.summary),
      el("td", { className: "st-" + r.status }, r.status + (r.decided_by ? " by " + r.decided_by : "")),
      el("td", {}, el("a", { className: "btn small" + (r.open ? "" : " secondary"), href: "/assistants/requests/" + r.request_id }, r.open ? "Review" : "Open")))));
  }

  async function loadHistory() {
    const q = new URLSearchParams({ limit: "100" });
    if ($("f-conn").value) q.set("grant_id", $("f-conn").value);
    if ($("f-status").value) q.set("status", $("f-status").value);
    if ($("f-risk").value) q.set("risk", $("f-risk").value);
    if ($("f-tool").value.trim()) q.set("tool", $("f-tool").value.trim());
    const rows = await api("GET", "/api/assistants/history?" + q);
    const body = $("hist-body");
    if (!rows.length) { body.replaceChildren(el("tr", {}, el("td", { colSpan: 6 }, "No calls yet."))); return; }
    body.replaceChildren(...rows.map((c) => {
      const args = Object.keys(c.args).length ? JSON.stringify(c.args, null, 1) : "";
      return el("tr", {},
        el("td", {}, when(c.at)), el("td", {}, c.label),
        el("td", {}, c.tool), el("td", { className: "risk-" + c.risk }, c.risk),
        el("td", { className: "st-" + c.status }, c.status + (c.decided_by ? " (" + c.decided_by + ")" : "")),
        el("td", {}, c.summary, c.approval_token ? el("div", {}, el("a", { href: "/assistants/requests/" + c.approval_token }, "request")) : "",
          args ? el("details", { className: "args" }, el("summary", {}, "arguments"), el("pre", {}, args)) : ""));
    }));
  }

  function refresh() { return Promise.all([loadGrants(), loadRequests(), loadHistory()]); }

  $("copy-url").addEventListener("click", () => {
    navigator.clipboard.writeText($("mcp-url").textContent).then(() => toast("Copied", "ok"));
  });
  for (const id of ["f-conn", "f-status", "f-risk"]) $(id).addEventListener("change", loadHistory);
  $("f-tool").addEventListener("input", () => { clearTimeout($("f-tool")._t); $("f-tool")._t = setTimeout(loadHistory, 300); });

  $("pt-make").addEventListener("click", async () => {
    const label = $("pt-label").value.trim();
    if (!label) { toast("Give the token a name", "error"); return; }
    const made = await api("POST", "/api/assistants/grants", {
      label, role: $("pt-role").value, confirm_writes: $("pt-confirm").checked,
      expires_days: $("pt-expires").value ? Number($("pt-expires").value) : null,
    });
    $("pt-out").replaceChildren(el("div", { className: "newtoken" },
      el("strong", {}, "Copy it now, it is not shown again:"),
      el("pre", {}, made.token), el("div", { className: "msg" }, made.note)));
    $("pt-label").value = "";
    refresh();
  });

  if (window.OrchLive) OrchLive.on("mcp", refresh, { wait: 700 });
  loadOverview().then(refresh);
})();
