/* System (templates/system.html), to the System wireframe: banners for a schema upgrade or a
 * staged restore, the Update card (check, update with the updater or the command, follow the run,
 * roll back), the time zone, and this orchestrator's details. Restart in the header.
 * Data: /api/system/info and the display settings embedded at first paint; the update check
 * (GitHub) and the updater's state are read after. */
(function () {
  "use strict";
  const { $, el, clear, fill, api, fmt, confirm } = window.UI;
  const V = JSON.parse($("#initial").textContent);
  const toast = (m, k) => window.toast(m, k);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let info = V.info;
  const kv = (box, pairs) => fill(box, pairs.filter(Boolean).map(([k, v]) => [el("dt", {}, k), el("dd", {}, v == null || v === "" ? "—" : v)]));
  const mono = (t) => el("span", { class: "mono" }, t);
  function duration(s) {
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? d + " d " + h + " h" : h ? h + " h " + m + " min" : m + " min";
  }

  // --- restart (the header, a staged restore) ---------------------------------------------------
  // The service stops answering, then answers again: reload then. Never longer than 4 minutes.
  async function waitForRestart(what) {
    toast(what + "… this page reloads when it is back.");
    let sawDown = false; const started = Date.now();
    while (Date.now() - started < 240000) {
      await sleep(2000);
      try { const r = await fetch("/healthz", { cache: "no-store" }); if (r.ok && (sawDown || Date.now() - started > 15000)) { location.reload(); return; } } catch (_e) { sawDown = true; }
    }
    toast("It has not come back after 4 minutes: check the container on the host.", "error");
  }
  async function restart(title) {
    if (!await confirm({ title, body: "Pages and wakes in flight are interrupted for a few seconds. Docker's restart policy starts it again.", action: "Restart", danger: false })) return;
    try { await api.post("/api/system/restart", {}); } catch (_e) { return; }
    waitForRestart("Restarting");
  }
  $("#restart-btn").addEventListener("click", () => restart("Restart the orchestrator?"));

  // --- banners and this orchestrator -------------------------------------------------------------
  function drawInfo() {
    const i = info;
    const box = clear($("#banners"));
    if (i.schema.pending) {
      box.append(el("div", { class: "banner sys-banner" }, el("span", { class: "pill warn" }, "upgrade"), " ",
        el("strong", {}, "The database needs upgrading"), " to this version's schema (" + (i.schema.current || "no revision") + " → " + i.schema.head + "). ",
        i.run_migrations_on_start ? "It upgrades by itself at the next start, or now: " : "A backup of the orchestrator is taken first. ",
        el("button", { type: "button", class: "small", onclick: migrate }, "Back up and upgrade")));
    }
    const p = i.pending_restore;
    if (p) {
      box.append(el("div", { class: "banner sys-banner" }, el("span", { class: "pill warn" }, "restore"), " ",
        el("strong", {}, "A restore is staged. "), "Restoring " + p.backup + " replaces the database" + (p.has_registry ? " and services.yaml" : "") + " when the orchestrator restarts; what is there now is backed up first. ",
        el("button", { type: "button", class: "danger-solid small", onclick: () => restart("Restart now and restore " + p.backup + "?") }, "Restart and restore"), " ",
        el("button", { type: "button", class: "secondary small", onclick: cancelRestore }, "Cancel")));
    }
    fill($("#mode-pill"), el("span", { class: "pill " + (i.dry_run ? "warn" : "ok") }, i.dry_run ? "dry run" : "live"));
    $("#mode-text").textContent = i.dry_run ? "DRY_RUN on: nothing is changed on the real systems" : "DRY_RUN off: changes reach the real systems";
    kv($("#info-kv"), [
      ["Version", mono(i.version + (i.git_sha ? " (" + i.git_sha.slice(0, 7) + ")" : ""))],
      ["Uptime", duration(i.uptime_s)],
      ["Image", i.image ? mono(i.image) : el("span", { class: "faint" }, "not reported (set ORCHESTRATOR_IMAGE in .env)")],
      ["Runtime", "Python " + i.python + " · " + i.platform.split("-")[0]],
      ["Database", [mono(i.database), el("span", { class: "faint" }, " · " + fmt.bytes(i.database_bytes))]],
      ["Schema", mono((i.schema.current || "none") + (i.schema.pending ? " → needs " + i.schema.head : " (up to date)"))],
      ["Registry", [mono(i.registry), i.registry_exists ? null : el("span", { class: "pill warn" }, "missing")]],
      ["Its backups", [i.self_backup.enabled ? "every " + i.self_backup.interval_hours + " h, keep " + i.self_backup.keep : "off", " · ", el("a", { href: "/backup-policies?tab=orchestrator" }, "on Backups → Orchestrator")]],
    ]);
  }
  async function reloadInfo() { try { info = await api.get("/api/system/info", { suppressToast: true }); drawInfo(); } catch (_e) { /* keep what is shown */ } }
  async function migrate() {
    try { const r = await api.post("/api/system/migrate", undefined, { suppressToast: true }); toast(r.message + (r.backup ? " Backup: " + r.backup : ""), "ok"); } catch (e) { toast(e.message, "error"); }
    reloadInfo();
  }
  async function cancelRestore() { try { await api.del("/api/system/restore"); } catch (_e) { /* toast */ } reloadInfo(); }

  // --- time zone ----------------------------------------------------------------------------------
  const tz = $("#tz-input");
  function showNow(zone) { $("#tz-now").textContent = "It is now " + fmt.time(new Date().toISOString()) + " in " + zone + "."; }
  tz.value = V.display.timezone;
  showNow(V.display.timezone);
  tz.addEventListener("focus", () => {
    if ($("#tz-list").childElementCount) return;
    api.get("/api/display/timezones", { suppressToast: true }).then((zones) => fill($("#tz-list"), zones.map((z) => el("option", { value: z }))), () => {});
  }, { once: true });
  async function saveTz(value) {
    try {
      const d = await api.put("/api/display", { timezone: value }, { suppressToast: true });
      window.OrchTime.set(d.timezone); tz.value = d.timezone; showNow(d.timezone);
      toast("Saved. Backup schedules now run in " + d.timezone + ".", "ok");
    } catch (e) { toast(e.message, "error"); }
  }
  $("#tz-save").addEventListener("click", () => saveTz(tz.value.trim()));
  $("#tz-browser").addEventListener("click", () => { tz.value = Intl.DateTimeFormat().resolvedOptions().timeZone; tz.focus(); });
  $("#tz-default").addEventListener("click", () => saveTz(""));

  // --- update ---------------------------------------------------------------------------------------
  let check = null; let updater = null; let following = null; let checkedAt = null;
  const FINISHED = ["succeeded", "rolled_back", "failed"];
  function drawUpdate() {
    const pillBox = $("#update-pill");
    if (!check) { fill(pillBox, el("span", { class: "pill off" }, "checking")); return; }
    fill(pillBox, check.available === true ? el("span", { class: "pill pending" }, "newer build")
      : check.available === false ? el("span", { class: "pill ok" }, "up to date") : el("span", { class: "pill off" }, "cannot tell"));
    $("#update-age").textContent = checkedAt ? "checked " + fmt.ago(checkedAt) : "";
    kv($("#update-kv"), [
      ["Running", check.current ? mono(check.current) : el("span", { class: "faint" }, "this image does not say which build it is")],
      ["Newest", check.latest ? [mono(check.channel + " @ " + check.latest), check.url ? [" ", el("a", { href: check.url, target: "_blank", rel: "noopener" }, "what changed")] : null] : el("span", { class: "faint" }, check.message)],
      ["Updater", updater && updater.online ? [el("span", { class: "pill ok" }, "running"), el("span", { class: "faint" }, " · puts the old build back by itself if the new one does not start")]
        : [el("span", { class: "pill off" }, "not running"), el("span", { class: "faint" }, " · install it with sh scripts/install.sh for one-click updates with rollback")]],
      check.available == null && check.latest ? ["", el("span", { class: "faint" }, check.message)] : null,
    ]);
    fill($("#update-actions"),
      check.configured ? el("button", { type: "button", class: check.available ? null : "secondary", onclick: updateNow }, check.available ? "Update now" : "Update anyway") : null,
      el("button", { type: "button", class: "secondary", onclick: () => doCheck(true) }, "Check again"),
      el("button", { type: "button", class: "ghost", onclick: toggleLog }, "Last update's log"));
    const manual = $("#update-manual");
    manual.hidden = !!check.configured;
    manual.textContent = check.configured ? "" : "Updating from this page is not set up (see docs/system.md). On the Docker host:\n\n  " + check.manual_steps.join("\n  ");
  }
  async function doCheck(refresh) {
    try { check = await api.get("/api/system/update" + (refresh ? "?refresh=true" : ""), { suppressToast: true }); checkedAt = new Date().toISOString(); updater = check.updater; }
    catch (e) { check = { available: null, current: "", latest: "", channel: "", message: e.message, configured: false, manual_steps: [] }; }
    drawUpdate();
    if (updater && updater.status) renderRun(updater.status);
    if (updater && updater.running && updater.status) follow(updater.status.id);
  }
  function renderRun(st) {
    const box = $("#update-progress");
    if (!st) { box.hidden = true; return; }
    box.hidden = false;
    const running = st.state === "running";
    const what = st.action === "rollback" ? "Rollback" : "Update";
    const b = $("#update-banner");
    b.className = "sys-run " + (running ? "busy" : st.state === "succeeded" ? "ok" : "fail");
    b.textContent = (running ? what + " in progress: " + (st.step || "starting")
      : st.state === "succeeded" ? what + " done: " + st.message
      : st.state === "rolled_back" ? what + " failed; the previous build was put back: " + st.reason
      : what + " failed: " + st.message + (st.reason ? " (" + st.reason + ")" : "")) + (st.finished_at ? " · " + fmt.short(st.finished_at) : "");
    const PILL = { OK: ["ok", "ok"], FAIL: ["fail", "failed"], SKIP: ["off", "skipped"], WARN: ["warn", "warning"] };
    fill($("#update-steps"), (st.steps || []).map((s) => {
      const [cls, word] = PILL[s.result] || ["pending", s.result.toLowerCase()];
      return el("li", {}, el("span", { class: "pill " + cls }, word), " " + s.name.replace(/_/g, " "), s.detail ? el("span", { class: "faint" }, " · " + s.detail) : null);
    }));
    $("#rollback-btn").hidden = !(st.action === "update" && st.state === "succeeded" && st.from_image !== st.to_image);
  }
  // Follow a run to its end. The orchestrator restarts in the middle, so failed polls are expected.
  async function follow(requestId) {
    if (following) return;
    following = requestId;
    const started = Date.now();
    while (Date.now() - started < 25 * 60 * 1000) {
      await sleep(2000);
      let o;
      try { o = await api.get("/api/system/update/status", { suppressToast: true }); } catch (_e) { continue; }
      const st = o.status;
      if (st && (!requestId || st.id === requestId)) {
        renderRun(st);
        if (FINISHED.includes(st.state)) {
          following = null;
          if (st.state === "succeeded" && st.from_image !== st.to_image) { toast("Reloading with the new build…", "ok"); await sleep(2500); location.reload(); }
          return;
        }
      }
    }
    following = null;
    toast("The updater has not reported back in 25 minutes: check it on the Docker host.", "error");
  }
  async function updateNow() {
    const viaUpdater = check && check.method === "updater";
    if (!await confirm({ title: "Back up, then update?", body: viaUpdater ? "If the new build does not come up healthy, the old one is put back by itself." : "The orchestrator restarts with the new build.", action: "Update", danger: false })) return;
    let r;
    try { r = await api.post("/api/system/update", undefined, { suppressToast: true }); }
    catch (_e) { waitForRestart("Updating"); return; } // the container may be replaced mid-request: that is the update working
    if (!r.ran || !r.ok) { toast(r.message + (r.output ? " " + r.output : ""), "error"); return; }
    if (r.method === "updater") { toast(r.message); follow(r.request_id); return; }
    waitForRestart("Updating");
  }
  $("#rollback-btn").addEventListener("click", async () => {
    if (!await confirm({ title: "Go back to the previous build?", body: "The data backup taken before the update is restored too, so changes made since then are lost. A backup of the current data is taken first.", typeName: "rollback", action: "Go back" })) return;
    try { const r = await api.post("/api/system/update/rollback", {}, { suppressToast: true }); toast(r.message + " Safety backup: " + r.safety_backup); follow(r.request_id); }
    catch (e) { toast(e.message, "error"); }
  });
  async function toggleLog() {
    const pre = $("#update-log");
    if (!pre.hidden) { pre.hidden = true; return; }
    try { pre.textContent = (await api.get("/api/system/update/log", { suppressToast: true })).log || "(empty)"; } catch (e) { pre.textContent = e.message; }
    pre.hidden = false;
  }

  drawInfo();
  drawUpdate();
  doCheck(false);
  setInterval(() => { if (checkedAt) $("#update-age").textContent = "checked " + fmt.ago(checkedAt); }, 30000);
})();
