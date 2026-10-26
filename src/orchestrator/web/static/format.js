/* Format routes: plan, then rewrite hand-written / migrated routes into the standard shape, one host
 * at a time. Everything comes from /api/sources/traefik/format/*. Built with textContent only -
 * nothing a remote system returns is ever put in as HTML. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, props, ...kids) => {
    const n = document.createElement(tag);
    if (props) for (const [k, v] of Object.entries(props)) { if (k === "className") n.className = v; else if (k === "text") n.textContent = v; else n[k] = v; }
    for (const k of kids) if (k !== null && k !== undefined && k !== false) n.append(k.nodeType ? k : document.createTextNode(String(k)));
    return n;
  };
  const code = (t) => el("code", null, t);
  const chip = (t, kind) => el("span", { className: "chip" + (kind ? " " + kind : "") }, t);
  const err = (e) => (e && e.message) || String(e);
  const api = (method, url, body) => apiFetch(method, url, body, { suppressToast: true });
  const msg = (id, text, kind) => { const m = $(id); m.textContent = text; m.className = "msg" + (kind ? " " + kind : ""); };
  async function busyButton(btn, fn) {
    btn.disabled = true;
    try { return await fn(); } finally { btn.disabled = false; }
  }

  // -- format ------------------------------------------------------------------------------------------------
  let plan = null;
  async function planFormat() {
    msg("fmt-msg", "Reading every route…");
        try { plan = await api("POST", "/api/sources/traefik/format/plan", hstsChoice() === null ? {} : { hsts: hstsChoice() }); }
    catch (e) { msg("fmt-msg", err(e), "error"); return; }
    if (plan.problem) { msg("fmt-msg", plan.problem, "error"); $("fmt-items").replaceChildren(); return; }
    const c = plan.counts;
    msg("fmt-msg", `${c.ready || 0} ready, ${c.standard || 0} already standard, ${c.blocked || 0} left alone, ${c.skipped || 0} skipped.`, "");
    $("fmt-every").disabled = !c.ready;
    $("fmt-every").textContent = c.ready ? `Format all ${c.ready} ready route(s)` : "Format all ready routes";
    const h = plan.hsts || {};
    $("fmt-hsts-note").textContent = h.available === false && hstsChoice() === true
      ? `Traefik has no "${h.middleware}" middleware yet: it is created with the first route that uses it.` : "";
    $("fmt-items").replaceChildren(...plan.items.map(itemCard));
  }
  function kv(label, value) { return el("div", null, el("dt", null, label), el("dd", null, value)); }
  function itemCard(i) {
    const card = el("div", { className: "fmt-item " + i.status });
    const head = el("h4", null);
    if (i.status === "ready") { const cb = el("input", { type: "checkbox", checked: true }); cb.dataset.host = i.hostname; head.append(cb, " "); }
    head.append(code(i.hostname), " ", i.service ? el("span", { className: "muted" }, i.service + " · ") : "", chip(i.status === "ready" ? i.action : i.status, i.status === "ready" ? "ok" : i.status === "blocked" ? "bad" : null));
    card.append(head);
    if (i.reasons.length) card.append(el("div", { className: "why" }, i.reasons.join(" · ")));
    if (i.status === "ready") {
      card.append(el("ul", null, ...i.changes.map((c) => el("li", null, c))));
      const cur = i.current, tgt = i.target;
      card.append(el("dl", { className: "kv" },
        kv("Now", `${cur.name} in ${cur.file || "(default file)"} · ${cur.entry_points.join(", ") || "-"} · ${cur.cert_resolver || "no resolver"} · redirect router: ${cur.redirect_router ? "yes" : "no"}`),
        kv("Becomes", `${tgt.name} in ${tgt.file || "(default file)"} · ${tgt.entry_points.join(", ")} · ${tgt.cert_resolver || "no resolver"} · redirect router: ${tgt.options.http_redirect ? "yes" : "no"}`)));
      if (cur.middlewares.length) {
        const row = el("div", { className: "checks" }, el("span", { className: "muted" }, "Middlewares kept: "));
        for (const m of cur.middlewares) {
          const dropped = i.dropped_middlewares.includes(m);
          const cb = el("input", { type: "checkbox", checked: !dropped }); cb.dataset.host = i.hostname; cb.dataset.mw = m;
          row.append(el("label", { className: "inline", title: dropped ? "an old redirect-to-HTTPS middleware: the redirect router replaces it" : "" }, cb, " " + m + (dropped ? " (old redirect)" : "") + "  "));
        }
        card.append(row);
      }
    }
    for (const w of i.warnings) card.append(el("div", { className: "msg error" }, w));
    return card;
  }
  const hostBoxes = () => [...document.querySelectorAll("#fmt-items input[type=checkbox][data-host]:not([data-mw])")];
  // null = keep what each route has
  function hstsChoice() { const v = $("fmt-hsts").value; return v === "on" ? true : v === "off" ? false : null; }
  $("fmt-hsts").addEventListener("change", () => busyButton($("fmt-replan"), planFormat));
  $("fmt-all").addEventListener("click", () => hostBoxes().forEach((b) => { b.checked = true; }));
  $("fmt-none").addEventListener("click", () => hostBoxes().forEach((b) => { b.checked = false; }));
  async function runFormat(chosen, button) {
    if (!plan) return;
    if (!chosen.length) { msg("fmt-msg", "Select at least one host.", "error"); return; }
    const hs = hstsChoice();
    const hsNote = hs === true ? " HSTS is added to each." : hs === false ? " HSTS is taken off each." : "";
    if (!confirm(`Rewrite ${chosen.length} route(s) now? Each is created beside the old one, checked, then the old one is removed; a failure puts it back and stops the run.${hsNote}`)) return;
    const items = chosen.map((h) => {
      const it = plan.items.find((x) => x.hostname === h);
      const mws = [...document.querySelectorAll(`#fmt-items input[data-mw][data-host="${CSS.escape(h)}"]`)];
      return { hostname: h, fingerprint: it.fingerprint, ...(mws.length ? { middlewares: mws.filter((b) => b.checked).map((b) => b.dataset.mw) } : {}) };
    });
    msg("fmt-msg", "Starting…");
    try {
      const r = await api("POST", "/api/sources/traefik/format/run", {
        items, stop_on_failure: $("fmt-stop").checked, allow_unverified: $("fmt-unverified").checked,
        ...(hs === null ? {} : { hsts: hs }),
      });
      await follow(r.run_id);
    } catch (e) { msg("fmt-msg", err(e), "error"); }
  }
  $("fmt-run").addEventListener("click", () => busyButton($("fmt-run"),
    () => runFormat(hostBoxes().filter((b) => b.checked).map((b) => b.dataset.host))));
  $("fmt-every").addEventListener("click", () => busyButton($("fmt-every"), () => {
    hostBoxes().forEach((b) => { b.checked = true; });
    return runFormat(hostBoxes().map((b) => b.dataset.host));
  }));
  async function follow(runId) {
    const ol = $("fmt-steps");
    ol.hidden = false;
    for (let n = 0; n < 3600; n++) {
      const run = await api("GET", "/api/runs/" + runId);
      ol.replaceChildren(...run.steps.map((s) => {
        const d = s.detail || {};
        const note = d.error || d.reason || d.message || d.to || d.ref || d.state || "";
        const li = el("li", { className: s.status === "failed" ? "failed" : s.status === "skipped" ? "muted" : "ok" }, s.name + (note ? " — " + note : ""));
        return li;
      }));
      if (run.status !== "running") {
        msg("fmt-msg", run.status === "succeeded" ? "Done." : "Finished with problems: " + (run.error || run.status), run.status === "succeeded" ? "ok" : "error");
        planFormat();
        return;
      }
      await new Promise((res) => setTimeout(res, 1000));
    }
  }


  $("fmt-replan").addEventListener("click", () => busyButton($("fmt-replan"), planFormat));
  busyButton($("fmt-replan"), planFormat);
})();
