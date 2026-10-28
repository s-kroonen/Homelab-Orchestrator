/* The frame around every page (base.html): the menu toggle, the global search, the pending pill,
 * the live indicator and the user menu. Needs ui.js and live.js. */
(function () {
  "use strict";
  const { $, el, clear, api, menu } = window.UI;

  // --- sidebar on a phone -------------------------------------------------------------------
  const sidebar = $(".sidebar");
  const toggle = sidebar && $(".nav-toggle", sidebar);
  function setNav(open) {
    if (!sidebar) return;
    sidebar.classList.toggle("open", open);
    if (toggle) toggle.setAttribute("aria-expanded", String(open));
  }
  if (toggle) toggle.addEventListener("click", () => setNav(!sidebar.classList.contains("open")));
  const more = $("#phone-more");
  if (more) more.addEventListener("click", (ev) => { ev.preventDefault(); setNav(true); window.scrollTo({ top: 0 }); });

  // --- live indicator ------------------------------------------------------------------------
  const livePill = $("#live-pill");
  function liveState() {
    if (!livePill || !window.OrchLive) return;
    const on = window.OrchLive.connected;
    livePill.className = "pill " + (on ? "ok" : "off");
    livePill.title = on ? "Live: this page updates as things change" : "Not live: this page refreshes on a timer";
  }
  if (window.OrchLive) window.OrchLive.on("pending", () => {}); // opens the stream on every page
  setInterval(liveState, 2000); liveState();

  // --- user menu -----------------------------------------------------------------------------
  const userBtn = $("#user-btn");
  if (userBtn) userBtn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    menu(userBtn, [
      { label: "Admins & devices", href: "/admins" },
      { label: "System", href: "/system" },
      "-",
      { label: "Log out", onClick: async () => { await fetch("/api/auth/logout", { method: "POST" }); location.href = "/login"; } },
    ]);
  });

  // --- global search ----------------------------------------------------------------------------
  const input = $("#global-search");
  const box = $("#search-results");
  let index = null; let active = -1; let shown = [];
  async function loadIndex() {
    if (index) return index;
    try { index = (await api.get("/api/search", { suppressToast: true })).items; } catch (_e) { index = []; }
    return index;
  }
  if (window.OrchLive) window.OrchLive.on("registry", () => { index = null; }, { wait: 1000 });
  function score(item, q) {
    const l = item.label.toLowerCase();
    if (l === q) return 0;
    if (l.startsWith(q)) return 1;
    if (l.includes(q)) return 2;
    if ((item.sub || "").toLowerCase().includes(q)) return 3;
    return -1;
  }
  async function search() {
    const q = input.value.trim().toLowerCase();
    if (!q) { close(); return; }
    const items = await loadIndex();
    shown = items.map((it) => [score(it, q), it]).filter(([s]) => s >= 0).sort((a, b) => a[0] - b[0]).slice(0, 12).map(([, it]) => it);
    active = shown.length ? 0 : -1;
    draw();
  }
  function draw() {
    clear(box);
    if (!shown.length) box.append(el("div", { class: "none" }, "Nothing found"));
    shown.forEach((it, i) => box.append(el("a", {
      href: it.href, role: "option", class: i === active ? "active" : null, "aria-selected": String(i === active),
    }, el("span", { class: "kind" }, it.kind), el("span", {}, it.label), it.sub ? el("span", { class: "faint", style: { fontSize: "12px" } }, it.sub) : null)));
    box.hidden = false; input.setAttribute("aria-expanded", "true");
  }
  function close() { if (box) { box.hidden = true; input.setAttribute("aria-expanded", "false"); } }
  if (input) {
    input.addEventListener("input", search);
    input.addEventListener("focus", () => { loadIndex(); if (input.value.trim()) search(); });
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        if (!shown.length) return;
        active = (active + (ev.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length; draw();
      } else if (ev.key === "Enter" && shown[active]) {
        ev.preventDefault(); location.href = shown[active].href;
      } else if (ev.key === "Escape") { input.value = ""; close(); input.blur(); }
    });
    document.addEventListener("click", (ev) => { if (!ev.target.closest(".search")) close(); });
    document.addEventListener("keydown", (ev) => {
      if (ev.key !== "/" || ev.ctrlKey || ev.metaKey || ev.altKey) return;
      const t = ev.target;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      ev.preventDefault(); input.focus(); input.select();
    });
  }

  // --- pending pill: saved but not written to services.yaml or pushed yet ---------------------
  const wrap = $("#pending-pill");
  const btn = $("#pending-btn");
  const pop = $("#pending-pop");
  let pending = null;
  function drawPending() {
    if (!wrap || !pending) return;
    const failed = (pending.push_failed || []).length > 0 || !!pending.save_error;
    if (!pending.count && !pending.running) { wrap.hidden = true; pop.hidden = true; return; }
    wrap.hidden = false;
    wrap.classList.toggle("fail", failed);
    btn.textContent = pending.running ? "Saving…" : (failed ? "⚠ " : "⟳ ") + pending.count + " pending";
    clear(pop).append(
      el("strong", {}, failed ? "Not everything went through" : "Saved, not everywhere yet"),
      el("ul", {}, (pending.items || []).map((t) => el("li", {}, t))),
      el("div", { class: "faint", style: { fontSize: "12px", marginBottom: "10px" } },
        pending.autosave ? "Changes are written to services.yaml and pushed to Traefik Manager by themselves; this is what did not work."
          : "Automatic saving is off (REGISTRY_AUTOSAVE=false): write and push here."),
      el("div", { style: { display: "flex", gap: "8px", justifyContent: "flex-end" } },
        el("a", { class: "btn secondary small", href: "/proxy-hosts" }, "Proxy hosts"),
        el("button", { type: "button", class: "small", disabled: !!pending.running, onclick: retry }, pending.autosave ? "Retry" : "Save and push")));
  }
  async function loadPending() {
    try { pending = await api.get("/api/pending", { suppressToast: true }); drawPending(); } catch (_e) { /* signed out */ }
  }
  async function retry() {
    pending = Object.assign({}, pending, { running: true }); drawPending();
    try {
      pending = await api.post("/api/pending/retry");
      window.toast(pending.count ? "Still pending: " + pending.items.join("; ") : "Saved and pushed", pending.count ? "error" : "ok");
    } catch (_e) { /* toast shown */ }
    drawPending();
  }
  if (btn) {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      pop.hidden = !pop.hidden; btn.setAttribute("aria-expanded", String(!pop.hidden));
    });
    document.addEventListener("click", (ev) => { if (!ev.target.closest("#pending-pill")) { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); } });
    loadPending();
    if (window.OrchLive) window.OrchLive.on(["pending", "registry"], loadPending, { wait: 2500 });
  }
  window.OrchShell = { refreshPending: loadPending };
})();
