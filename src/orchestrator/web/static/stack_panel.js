/* The service page's "On / off & deploys" tab: what is really switched on (proxy hosts,
 * Portainer stack, guest), the one switch, proxy-host linking, and the deploy method with its
 * guards, history and webhook. Everything is built with textContent; nothing from Portainer or a
 * deploy step is ever put in as HTML. Relies on SERVICE, apiFetch, toast and fmtTime from the page. */
(function () {
  "use strict";
  if (typeof SERVICE === "undefined") return;
  const slug = SERVICE.slug;
  const base = "/api/services/" + encodeURIComponent(slug);
  const $ = (id) => document.getElementById(id);
  let busy = 0;
  window.StackPanel = { busy: () => busy > 0 };

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  const err = (e) => (e && e.message) || String(e);
  function msg(id, text, kind) { const m = $(id); m.textContent = text; m.className = "msg" + (kind ? " " + kind : ""); }
  async function guard(fn) { busy++; try { return await fn(); } finally { busy--; } }
  const ctl = SERVICE.controller || {};
  const hasStack = ctl.type === "portainer" && !!ctl.stack;
  const isRoot = !SERVICE.parent;

  // -- on / off -------------------------------------------------------------------------------
  const STATE_BADGE = { on: "healthy", off: "failed", mixed: "warn", unknown: "unknown", none: "disabled" };
  let linked = null;

  function setBadge(node, state) {
    node.className = "badge " + (STATE_BADGE[state] || "unknown");
    node.textContent = state === "none" ? "nothing linked" : state;
  }
  function partBadge(on) {
    return el("span", "badge " + (on === true ? "healthy" : on === false ? "failed" : "unknown"), on === true ? "on" : on === false ? "off" : "?");
  }

  function renderLinked(st) {
    linked = st;
    setBadge($("ls-state"), st.state);
    const tabBadge = $("stack-tab-state");
    tabBadge.hidden = st.state === "none";
    setBadge(tabBadge, st.state);
    $("ls-drift").textContent = st.host_down
      ? "Nothing is running here: " + st.host_down + "." + (st.served ? " It is meant to be on, so wake it." : "")
      : st.drift
      ? "Not what was asked for: " + st.drift + (st.unknown.length ? "" : ".")
      : st.state === "none" ? "Nothing is linked to this service yet: no proxy host, controller or guest." : "";
    $("ls-drift").className = "msg" + (st.drift ? " error" : "");
    const body = $("ls-body");
    if (!st.parts.length) {
      const tr = el("tr"); const td = el("td", "muted", "No linked proxy host, stack, container or guest."); td.colSpan = 4;
      tr.append(td); body.replaceChildren(tr);
    } else {
      body.replaceChildren(...st.parts.map((p) => {
        const tr = el("tr");
        const name = el("td"); name.append(el("strong", "", p.label));
        if (p.deliberate) name.append(document.createTextNode(" "), el("span", "chip", "switched off on its own"));
        const now = el("td"); now.append(partBadge(p.on));
        const act = el("td");
        if (p.open_url) { const a = el("a", "", "Open in Portainer"); a.href = p.open_url; a.target = "_blank"; a.rel = "noopener"; act.append(a); }
        tr.append(name, now, el("td", "", p.detail), act);
        return tr;
      }));
    }
    const mixed = st.state === "mixed";
    $("ls-apply-on").hidden = !mixed; $("ls-apply-off").hidden = !mixed;
    const hasGuest = st.parts.some((p) => p.kind === "guest");
    $("ls-guest-wrap").hidden = !(isRoot && hasGuest);
    $("ls-children-wrap").hidden = !SERVICE.children || !SERVICE.children.length;
    $("ls-stack-controls").hidden = !hasStack;
  }

  async function loadLinked() {
    try { renderLinked(await apiFetch("GET", base + "/linked", undefined, { suppressToast: true })); }
    catch (e) { msg("ls-msg", err(e), "error"); }
  }

  function showSteps(listId, steps) {
    const ol = $(listId);
    ol.hidden = !steps || !steps.length;
    ol.replaceChildren(...(steps || []).map((s) => {
      const d = s.detail || {};
      const note = d.error || d.reason || d.note || d.message || "";
      const li = el("li", s.status === "failed" ? "failed" : s.status === "skipped" ? "" : "ok", s.name + (note ? " — " + note : ""));
      if (s.status === "skipped") li.classList.add("muted");
      return li;
    }));
  }

  async function doSwitch(state, force) {
    const body = { state };
    if (state === "off") {
      body.include_guest = !$("ls-guest-wrap").hidden && $("ls-guest").checked;
      const parts = [`Switch ${slug} off?`, "Its proxy hosts stop being served and its stack stops."];
      if ((SERVICE.children || []).length) parts.push("Everything below it goes first.");
      if (body.include_guest) parts.push("The Proxmox guest is stopped too.");
      if (!force && !confirm(parts.join("\n"))) return;
    } else {
      body.restore_children = !$("ls-children-wrap").hidden && $("ls-children").checked;
    }
    msg("ls-msg", "Working…");
    await guard(async () => {
      try {
        const r = await apiFetch("POST", base + "/switch", body, { suppressToast: true });
        showSteps("ls-steps", r.steps);
        if (r.state) renderLinked(r.state);
        msg("ls-msg", r.ok ? r.message : "Failed: " + r.message, r.ok ? "ok" : "error");
        toast(r.ok ? `${slug} is ${r.state ? r.state.state : state}` : "Failed: " + r.message, r.ok ? "ok" : "error");
      } catch (e) { msg("ls-msg", err(e), "error"); toast(err(e), "error"); }
    });
  }
  $("ls-on").addEventListener("click", () => doSwitch("on"));
  $("ls-off").addEventListener("click", () => doSwitch("off"));
  $("ls-apply-on").addEventListener("click", () => doSwitch("on"));
  $("ls-apply-off").addEventListener("click", () => doSwitch("off"));
  $("ls-refresh").addEventListener("click", loadLinked);
  for (const b of document.querySelectorAll("[data-stack-action]")) {
    b.addEventListener("click", async () => {
      const action = b.dataset.stackAction;
      if (action !== "start" && !confirm(`${action} stack ${ctl.stack} in Portainer?\nThe proxy hosts are not touched - use the switch for that.`)) return;
      await guard(async () => {
        try {
          const r = await apiFetch("POST", `/api/stacks/${ctl.endpoint}/${encodeURIComponent(ctl.stack)}/${action}`, undefined, { suppressToast: true });
          msg("ls-msg", r.message, "ok");
        } catch (e) { msg("ls-msg", err(e), "error"); }
        loadLinked();
      });
    });
  }

  // -- proxy hosts ------------------------------------------------------------------------------
  function hostChip(name, label, onClick) {
    const w = el("span", "chip");
    w.append(document.createTextNode(name + " "));
    const b = el("button", "secondary small", label); b.type = "button"; b.addEventListener("click", onClick);
    w.append(b); return w;
  }
  async function hostCall(method, path, body) {
    try {
      await apiFetch(method, path, body, { suppressToast: true });
      msg("ph-msg", "Done. Push it to Traefik Manager when ready (Save to YAML does).", "ok");
    } catch (e) { msg("ph-msg", err(e), "error"); }
    loadHosts(); loadLinked();
  }
  async function loadHosts() {
    try {
      const c = await apiFetch("GET", base + "/proxy-host-candidates", undefined, { suppressToast: true });
      const row = $("ph-linked");
      row.replaceChildren(...(c.linked.length ? c.linked.map((h) =>
        hostChip(h, "Unlink", () => hostCall("DELETE", `${base}/proxy-hosts/${encodeURIComponent(h)}`)))
        : [el("span", "muted", "none linked")]));
      const mine = new Set(c.linked);
      const cands = c.candidates.filter((h) => !mine.has(h.hostname));
      $("ph-cands-wrap").hidden = !cands.length;
      $("ph-cands").replaceChildren(...cands.map((h) =>
        hostChip(h.hostname + (h.linked_to ? ` (on ${h.linked_to})` : ""), h.linked_to ? "Move here" : "Link",
          () => hostCall("POST", base + "/proxy-hosts", { hostname: h.hostname, force: !!h.linked_to }))));
    } catch (e) { msg("ph-msg", err(e), "error"); }
  }
  $("ph-link").addEventListener("click", () => {
    const name = $("ph-name").value.trim();
    if (!name) { msg("ph-msg", "Type the proxy host's name.", "error"); return; }
    hostCall("POST", base + "/proxy-hosts", { hostname: name, force: $("ph-force").checked });
  });

  // -- deploys ----------------------------------------------------------------------------------
  const PRESETS = {
    none: { pre: false, backup: false, age: 24, take: true, post: false },
    probes: { pre: true, backup: false, age: 24, take: true, post: true },
    "probes+backup": { pre: true, backup: true, age: 24, take: true, post: true },
  };
  function fillDeployForm(d) {
    const has = d && Object.keys(d).length > 0;
    const g = has ? d.guard : null;
    $("dp-pre-probes").checked = has ? !!g.pre.probes : false;
    $("dp-backup").checked = has ? !!g.pre.backup : false;
    $("dp-backup-age").value = has && g.pre.backup ? g.pre.backup.max_age_h : 24;
    $("dp-take").checked = has && g.pre.backup ? !!g.pre.backup.take_if_older : true;
    $("dp-post-probes").checked = has ? !!g.post.probes : true;
    $("dp-timeout").value = has ? g.post.timeout_s : 180;
    $("dp-settle").value = has ? g.post.settle_s : 30;
    $("dp-onfail").value = has ? d.on_failure : "notify";
    $("dp-pull").checked = has ? !!d.pull_image : true;
    $("dp-prune").checked = has ? !!d.prune : false;
    $("dp-enabled").checked = has ? !!d.enabled : true;
    const hook = has ? d.trigger.webhook : null;
    $("dp-hook").value = hook ? hook.provider : "";
    $("dp-branch").value = hook ? hook.branch : "";
    $("dp-remove").hidden = !has;
  }
  function readDeployForm() {
    const backup = $("dp-backup").checked
      ? { max_age_h: parseInt($("dp-backup-age").value, 10) || 24, take_if_older: $("dp-take").checked } : null;
    const provider = $("dp-hook").value;
    return {
      enabled: $("dp-enabled").checked,
      trigger: { webhook: provider ? { provider, branch: $("dp-branch").value.trim() } : null },
      guard: {
        pre: { probes: $("dp-pre-probes").checked, backup },
        post: { probes: $("dp-post-probes").checked, timeout_s: parseInt($("dp-timeout").value, 10) || 180, settle_s: parseInt($("dp-settle").value, 10) || 0 },
      },
      on_failure: $("dp-onfail").value,
      pull_image: $("dp-pull").checked,
      prune: $("dp-prune").checked,
    };
  }
  $("dp-preset").addEventListener("change", () => {
    const p = PRESETS[$("dp-preset").value];
    if (!p) return;
    $("dp-pre-probes").checked = p.pre; $("dp-backup").checked = p.backup; $("dp-backup-age").value = p.age;
    $("dp-take").checked = p.take; $("dp-post-probes").checked = p.post;
  });
  async function putDeploy(deploy) {
    return apiFetch("PUT", "/api/registry/services/" + encodeURIComponent(slug), { deploy }, { suppressToast: true });
  }
  $("dp-save").addEventListener("click", async () => {
    try {
      const s = await putDeploy(readDeployForm());
      SERVICE.deploy = s.deploy;
      msg("dp-msg", "Applied. Save to YAML to keep it.", "ok");
      afterDeployChange();
    } catch (e) { msg("dp-msg", err(e), "error"); }
  });
  $("dp-remove").addEventListener("click", async () => {
    if (!confirm("Remove this service's deploy method? The stack itself is not touched.")) return;
    try { const s = await putDeploy({}); SERVICE.deploy = s.deploy; fillDeployForm({}); msg("dp-msg", "Removed.", "ok"); afterDeployChange(); }
    catch (e) { msg("dp-msg", err(e), "error"); }
  });

  function afterDeployChange() {
    const has = SERVICE.deploy && Object.keys(SERVICE.deploy).length > 0;
    $("dp-actions").hidden = !(hasStack && has);
    $("dp-remove").hidden = !has;
    loadHook();
  }

  function runLine(r) {
    const trig = (r.steps.find((s) => s.name === "trigger") || { detail: {} }).detail;
    return { source: trig.source || "", status: r.status };
  }
  const RESULT = { succeeded: "healthy", failed: "failed", skipped: "warn", running: "unknown" };
  async function loadHistory() {
    try {
      const rows = await apiFetch("GET", base + "/deploys?limit=15", undefined, { suppressToast: true });
      const body = $("dp-history");
      if (!rows.length) { const tr = el("tr"); const td = el("td", "muted", "No deploys yet."); td.colSpan = 4; tr.append(td); body.replaceChildren(tr); return; }
      body.replaceChildren(...rows.flatMap((r) => {
        const info = runLine(r);
        const tr = el("tr");
        const res = el("td"); res.append(el("span", "badge " + (RESULT[r.status] || "unknown"), r.status));
        if (r.error) res.append(document.createTextNode(" " + r.error));
        const more = el("button", "secondary small", "Steps"); more.type = "button";
        const act = el("td"); act.append(more);
        tr.append(el("td", "", fmtTime(r.started_at)), el("td", "", info.source + (r.steps.length && r.steps[0].detail && r.steps[0].detail.actor ? " (" + r.steps[0].detail.actor + ")" : "")), res, act);
        const detail = el("tr"); detail.hidden = true;
        const dtd = el("td"); dtd.colSpan = 4;
        const ol = el("ol", "steps");
        r.steps.forEach((s) => {
          const d = s.detail || {};
          const li = el("li", s.status === "failed" ? "failed" : s.status === "skipped" ? "muted" : "ok", s.name + (d.error || d.reason || d.note ? " — " + (d.error || d.reason || d.note) : ""));
          ol.append(li);
        });
        dtd.append(ol); detail.append(dtd);
        more.addEventListener("click", () => { detail.hidden = !detail.hidden; });
        return [tr, detail];
      }));
    } catch (e) { $("dp-history").replaceChildren(); msg("dp-run-msg", err(e), "error"); }
  }

  async function startDeploy(body) {
    msg("dp-run-msg", "Starting…");
    await guard(async () => {
      let run;
      try { run = await apiFetch("POST", base + "/deploy", body, { suppressToast: true }); }
      catch (e) { msg("dp-run-msg", err(e), "error"); return; }
      msg("dp-run-msg", `Run #${run.run_id} started.`);
      loadHistory();
      for (let i = 0; i < 600; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        let cur;
        try { cur = await apiFetch("GET", "/api/deploys/" + run.run_id, undefined, { suppressToast: true }); } catch (e) { continue; }
        showSteps("dp-steps", cur.steps);
        if (cur.status !== "running") {
          const ok = cur.status === "succeeded";
          msg("dp-run-msg", ok ? (body.guards_only ? "The guards pass." : "Deployed.") : (cur.status === "skipped" ? "Blocked: " : "Failed: ") + (cur.error || ""), ok ? "ok" : "error");
          break;
        }
      }
      loadHistory(); loadLinked();
    });
  }
  $("dp-run").addEventListener("click", () => { if (confirm(`Redeploy ${slug} through its guards?`)) startDeploy({ guards: true }); });
  $("dp-guards").addEventListener("click", () => startDeploy({ guards: true, guards_only: true }));
  $("dp-unguarded").addEventListener("click", () => {
    const reason = prompt("Redeploy WITHOUT the checks before the deploy.\nWhy? (recorded in the audit log)");
    if (reason && reason.trim()) startDeploy({ guards: false, reason: reason.trim() });
  });

  // -- webhook ------------------------------------------------------------------------------------
  // -- the GitHub Actions block ------------------------------------------------------------------
  const gh = { plain: "", inline: "", name: "", path: "" };
  function renderGithub() {
    const useKey = $("gh-inline").checked && gh.inline;
    $("gh-block").textContent = useKey ? gh.inline : gh.plain;
    $("gh-intro").textContent = gh.plain
      ? `Save this as ${gh.path} in the repository. Its key is read from the repository secret ${gh.name}; ` +
        "every service has its own key, so each repository (or service) gets its own secret. The job fails, " +
        "with the reason, if the orchestrator cannot be reached, refuses the key, blocks the deploy, or the deploy does not end up healthy."
      : "";
  }
  $("gh-inline").addEventListener("change", renderGithub);
  $("gh-copy").addEventListener("click", async () => {
    const text = $("gh-block").textContent;
    try { await navigator.clipboard.writeText(text); msg("gh-msg", "Copied.", "ok"); }
    catch (e) {
      const t = el("textarea"); t.value = text; document.body.append(t); t.select();
      try { document.execCommand("copy"); msg("gh-msg", "Copied.", "ok"); } catch (e2) { msg("gh-msg", "Select the block and copy it by hand.", "error"); }
      t.remove();
    }
  });

  async function loadHook() {
    const has = SERVICE.deploy && Object.keys(SERVICE.deploy).length > 0;
    const hookCfg = has ? SERVICE.deploy.trigger.webhook : null;
    $("dp-hook-box").hidden = !hookCfg;
    if (!hookCfg) return;
    try {
      const d = await apiFetch("GET", base + "/deploy", undefined, { suppressToast: true });
      const w = d.webhook;
      $("dp-hook-text").textContent = `${w.provider}${w.branch ? ", branch " + w.branch : ""} → ${w.url}. ` + (w.secret_set ? "A secret is set." : "No secret yet: create one before pointing anything at it.");
      $("dp-hook-del").hidden = !w.secret_set;
      if (w.github) {
        gh.plain = w.github.workflow; gh.name = w.github.secret_name; gh.path = w.github.path;
        $("gh-warn").hidden = w.url_is_public;
        $("gh-warn").textContent = w.url_is_public ? "" : "No public address is configured (ORCHESTRATOR_PUBLIC_URL), so the block has a placeholder URL: replace YOUR-ORCHESTRATOR with the address the runner can reach.";
        renderGithub();
      }
      $("dp-hook-new").textContent = w.secret_set ? "Replace the secret" : "Create the secret";
    } catch (e) { $("dp-hook-text").textContent = err(e); }
  }
  $("dp-hook-new").addEventListener("click", async () => {
    if ($("dp-hook-new").textContent.startsWith("Replace") && !confirm("Replace the secret? The old one stops working.")) return;
    try {
      const r = await apiFetch("POST", base + "/deploy/webhook", undefined, { suppressToast: true });
      $("dp-hook-secret").hidden = false;
      $("dp-hook-secret-text").textContent = r.secret;
      $("dp-hook-setup").textContent = r.setup.join("\n");
      gh.inline = r.github_inline_key; gh.plain = r.github.workflow; gh.name = r.github.secret_name; gh.path = r.github.path;
      $("gh-inline").disabled = false; $("gh-inline").checked = false;
      $("dp-hook-setup").textContent += `\n\nGitHub: add a repository secret named ${r.github.secret_name} with the key above.`;
      renderGithub();
      loadHook();
    } catch (e) { toast(err(e), "error"); }
  });
  $("dp-hook-del").addEventListener("click", async () => {
    if (!confirm("Remove the secret? The webhook stops working until a new one is created.")) return;
    try { await apiFetch("DELETE", base + "/deploy/webhook", undefined, { suppressToast: true }); $("dp-hook-secret").hidden = true; loadHook(); }
    catch (e) { toast(err(e), "error"); }
  });

  // -- start-up -----------------------------------------------------------------------------------
  const hasDeploy = SERVICE.deploy && Object.keys(SERVICE.deploy).length > 0;
  if (!hasStack) {
    $("dp-intro").textContent = "A deploy method redeploys a Portainer stack. Link this service to one first: adopt it from the Portainer page, or give it a Portainer stack controller on the Overview tab.";
    $("ls-stack-controls").hidden = true;
  } else {
    $("dp-intro").textContent = "How this stack is redeployed, and what has to be true before and after. Portainer holds the stack, its Git repository and its environment; only the guards and history live here.";
    $("dp-settings").hidden = false;
    fillDeployForm(SERVICE.deploy || {});
    $("dp-actions").hidden = !hasDeploy;
  }
  loadLinked();
  loadHosts();
  loadHistory();
  loadHook();

  // Live: a deploy, a switch or a health change on this service (or below it) refreshes the state
  // table and the history without a reload; busy() keeps the page's own reload away meanwhile.
  if (window.OrchLive) {
    const mine = new Set([SERVICE.id, ...(SERVICE.children || []).map((c) => c.id)]);
    OrchLive.on(["run", "health", "registry"], (d) => {
      if (d.service_id !== undefined && d.service_id !== null && !mine.has(d.service_id)) return;
      loadLinked();
      loadHistory();
    }, { wait: 800 });
  }
})();
