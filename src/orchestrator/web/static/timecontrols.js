/* Grafana-style time controls (templates/_timecontrols.html).
 *
 * State lives in the URL (?range=1h&refresh=30s, via history.replaceState)
 * so a reload or a shared link keeps it; localStorage only supplies the
 * default for a visit whose URL carries none. Refreshing calls the page's
 * own onRefresh(state) — it re-fetches JSON and re-renders in place, never
 * location.reload(). Auto-refresh pauses while the tab is hidden and catches
 * up once it is visible again.
 *
 *   const tc = new TimeControls(document.querySelector("[data-tc]"), {
 *     onRefresh: async ({ range, refresh }) => { ... },
 *   });
 *   tc.start();
 */
(function () {
  "use strict";

  var RANGES = ["15m", "1h", "6h", "24h", "7d", "14d"];
  var REFRESH_MS = { off: 0, "10s": 10000, "30s": 30000, "1m": 60000, "5m": 300000 };

  function storageGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function storageSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* private mode etc. */ }
  }

  function agoText(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    if (s < 5) return "just now";
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    return Math.floor(s / 3600) + "h ago";
  }

  function TimeControls(root, opts) {
    this.root = root;
    this.onRefresh = (opts && opts.onRefresh) || function () {};
    this.rangeSel = root.querySelector("[data-tc-range]");
    this.refreshSel = root.querySelector("[data-tc-refresh]");
    this.reloadBtn = root.querySelector("[data-tc-reload]");
    this.updatedEl = root.querySelector("[data-tc-updated]");
    this.timer = null;
    this.pending = false;
    this.busy = false;
    this.lastUpdated = null;

    var params = new URLSearchParams(window.location.search);
    this.state = {
      range: pick(params.get("range"), RANGES, storageGet("tc.range"), root.dataset.defaultRange || "24h"),
      refresh: pick(params.get("refresh"), Object.keys(REFRESH_MS), storageGet("tc.refresh"), root.dataset.defaultRefresh || "off"),
    };
    this.rangeSel.value = this.state.range;
    this.refreshSel.value = this.state.refresh;
    this.writeUrl();

    var self = this;
    this.rangeSel.addEventListener("change", function () {
      self.state.range = self.rangeSel.value;
      storageSet("tc.range", self.state.range);
      self.writeUrl();
      self.refresh();
    });
    this.refreshSel.addEventListener("change", function () {
      self.state.refresh = self.refreshSel.value;
      storageSet("tc.refresh", self.state.refresh);
      self.writeUrl();
      self.schedule();
    });
    this.reloadBtn.addEventListener("click", function () { self.refresh(); });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && self.pending) {
        self.pending = false;
        self.refresh();
      }
    });
    window.setInterval(function () { self.tick(); }, 1000);
  }

  function pick(fromUrl, allowed, fromStorage, fallback) {
    if (fromUrl && allowed.indexOf(fromUrl) !== -1) return fromUrl;
    if (fromStorage && allowed.indexOf(fromStorage) !== -1) return fromStorage;
    return fallback;
  }

  /** Set (or with value "" / null, drop) one extra query param, e.g. slug. */
  TimeControls.prototype.setParam = function (name, value) {
    var params = new URLSearchParams(window.location.search);
    if (value) params.set(name, value); else params.delete(name);
    history.replaceState(null, "", window.location.pathname + "?" + params.toString());
  };

  TimeControls.prototype.getParam = function (name) {
    return new URLSearchParams(window.location.search).get(name) || "";
  };

  TimeControls.prototype.writeUrl = function () {
    var params = new URLSearchParams(window.location.search);
    params.set("range", this.state.range);
    params.set("refresh", this.state.refresh);
    history.replaceState(null, "", window.location.pathname + "?" + params.toString());
  };

  TimeControls.prototype.start = function () {
    return this.refresh();
  };

  TimeControls.prototype.refresh = async function () {
    if (this.busy) return;
    this.busy = true;
    window.clearTimeout(this.timer);
    document.body.classList.add("tc-loading");
    try {
      await this.onRefresh({ range: this.state.range, refresh: this.state.refresh });
      this.lastUpdated = Date.now();
      this.root.classList.remove("tc-error");
    } catch (err) {
      this.root.classList.add("tc-error");
      if (window.console) console.error("refresh failed", err);
    } finally {
      this.busy = false;
      document.body.classList.remove("tc-loading");
      this.tick();
      this.schedule();
    }
  };

  TimeControls.prototype.schedule = function () {
    window.clearTimeout(this.timer);
    var ms = REFRESH_MS[this.state.refresh] || 0;
    if (!ms) return;
    var self = this;
    this.timer = window.setTimeout(function () {
      if (document.hidden) {
        self.pending = true; // catch up on visibilitychange
        return;
      }
      self.refresh();
    }, ms);
  };

  TimeControls.prototype.tick = function () {
    if (!this.updatedEl) return;
    if (this.root.classList.contains("tc-error")) {
      this.updatedEl.textContent = "refresh failed";
    } else if (this.lastUpdated) {
      this.updatedEl.textContent = "updated " + agoText(Date.now() - this.lastUpdated);
    }
    // Keep any relative "N ago" timestamps on the page (status strip) live.
    var nodes = document.querySelectorAll("time[data-age][datetime]");
    for (var i = 0; i < nodes.length; i++) {
      var t = Date.parse(nodes[i].getAttribute("datetime"));
      if (!isNaN(t)) nodes[i].textContent = agoText(Date.now() - t);
    }
  };

  TimeControls.agoText = agoText;
  window.TimeControls = TimeControls;
})();
