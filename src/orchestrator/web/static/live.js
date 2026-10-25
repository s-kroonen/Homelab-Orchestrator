/* Live updates: one EventSource per page (GET /api/events), shared by whatever wants it.
 *
 *   OrchLive.on("run", (data) => ...)               // run | health | audit | alert | registry
 *   OrchLive.on(["run", "health"], fn, { wait: 1500 })   // debounced: fn runs once after 1.5 s of quiet
 *   OrchLive.connected                               // true while the stream is open
 *
 * Events are hints ("something changed"), never state: a handler re-reads what it shows from the
 * API. The browser reconnects by itself; a page keeps a slow fallback timer for when the stream
 * is blocked (a proxy that buffers it) or the change came from another process (the CLI). */
(function () {
  "use strict";
  const handlers = new Map(); // topic -> [{fn, wait, timer}]
  const state = { connected: false, source: null };
  const api = {
    get connected() { return state.connected; },
    on(topics, fn, opts) {
      const wait = (opts && opts.wait) || 0;
      for (const t of [].concat(topics)) {
        if (!handlers.has(t)) handlers.set(t, []);
        handlers.get(t).push({ fn, wait, timer: null });
      }
      start();
    },
  };
  window.OrchLive = api;

  function dispatch(topic, data) {
    for (const h of handlers.get(topic) || []) {
      if (!h.wait) { safe(h.fn, data); continue; }
      clearTimeout(h.timer);
      h.timer = setTimeout(() => safe(h.fn, data), h.wait);
    }
  }
  function safe(fn, data) { try { fn(data); } catch (e) { console.error("live handler", e); } }
  function dot(on) {
    const d = document.getElementById("live-dot");
    if (d) { d.classList.toggle("on", on); d.title = on ? "Live: this page updates as things change" : "Not live: this page refreshes on a timer"; }
  }

  function start() {
    if (state.source || !("EventSource" in window)) return;
    const src = new EventSource("/api/events");
    state.source = src;
    src.addEventListener("hello", () => { state.connected = true; dot(true); });
    src.onerror = () => { state.connected = false; dot(false); }; // the browser retries
    for (const topic of ["run", "health", "audit", "alert", "registry", "mcp"]) {
      src.addEventListener(topic, (ev) => {
        let data = {};
        try { data = JSON.parse(ev.data || "{}"); } catch (e) { /* a hint without data */ }
        dispatch(topic, data);
      });
    }
  }
  // a page that registers nothing still shows whether the stream works
  document.addEventListener("DOMContentLoaded", () => { if (document.getElementById("live-dot")) start(); });
})();
