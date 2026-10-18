/* /traffic: fetch GET /api/traffic/series and draw everything in place.
 *
 * Hand-written inline SVG, no library. Charts are re-laid out at the
 * container's real width (viewBox = pixels, so text stays crisp) and again on
 * resize. Every value a tooltip shows is also reachable without hovering:
 * legend totals, stat tiles and the per-service table carry it. Untrusted
 * strings (hostnames, service names) only ever go in via textContent.
 */
(function () {
  "use strict";

  var SVGNS = "http://www.w3.org/2000/svg";
  // Stack order bottom -> top; validated as an adjacent categorical set on
  // the card surface (see docs/traffic_metrics.md "Reading the charts").
  var STATUS = [
    { key: "s2xx", label: "2xx", cls: "c-2xx" },
    { key: "s3xx", label: "3xx", cls: "c-3xx" },
    { key: "s4xx", label: "4xx", cls: "c-4xx" },
    { key: "s5xx", label: "5xx", cls: "c-5xx" },
  ];
  var LATENCY = [
    { key: "avg_ms", label: "Avg", cls: "c-avg" },
    { key: "max_ms", label: "Max", cls: "c-max" },
  ];
  // Seconds between x-axis labels, aligned to local wall-clock time.
  var TICK_EVERY = { "15m": 300, "1h": 900, "6h": 3600, "24h": 14400, "7d": 86400, "14d": 172800 };

  // -- tiny helpers ---------------------------------------------------------

  function s(tag, attrs, parent) {
    var node = document.createElementNS(SVGNS, tag);
    for (var k in attrs) if (attrs[k] != null) node.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(node);
    return node;
  }
  function h(tag, cls, text, parent) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }
  function compact(n) {
    var a = Math.abs(n);
    if (a >= 1e6) return trim(n / 1e6) + "M";
    if (a >= 1e3) return trim(n / 1e3) + "K";
    return String(Math.round(n * 10) / 10);
  }
  function trim(x) { return (Math.abs(x) >= 10 ? x.toFixed(0) : x.toFixed(1)).replace(/\.0$/, ""); }
  function fmtInt(n) { return n == null ? "—" : Number(n).toLocaleString(); }
  function fmtMs(v) {
    if (v == null) return "—";
    if (v >= 1000) return trim(v / 1000) + " s";
    if (v >= 10) return Math.round(v) + " ms";
    return v.toFixed(1) + " ms";
  }
  function pct(part, whole) {
    if (!whole) return "";
    var p = (part / whole) * 100;
    return (p > 0 && p < 0.1 ? "<0.1" : p.toFixed(p >= 10 ? 0 : 1)) + "%";
  }
  function niceTicks(max, asInt) {
    if (!(max > 0)) return { top: 1, ticks: [0] };
    var raw = max / 4;
    var p = Math.pow(10, Math.floor(Math.log10(raw)));
    var f = raw / p;
    var step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
    if (asInt) step = Math.max(1, Math.ceil(step));
    var top = Math.ceil(max / step) * step;
    var ticks = [];
    for (var v = 0; v <= top + step / 2; v += step) ticks.push(v);
    return { top: top, ticks: ticks };
  }
  function hhmm(d) {
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
  }
  function xLabel(ms, range) {
    var d = new Date(ms);
    if (range === "7d" || range === "14d") return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    return hhmm(d);
  }
  function bucketTitle(ms, stepS) {
    var a = new Date(ms), b = new Date(ms + stepS * 1000);
    return a.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }) +
      " · " + hhmm(a) + "–" + hhmm(b);
  }
  function debounce(fn, ms) {
    var t;
    return function () { clearTimeout(t); t = setTimeout(fn, ms); };
  }

  // -- shared chart frame ---------------------------------------------------

  function frame(container, height, left) {
    var W = Math.max(260, Math.floor(container.clientWidth));
    var m = { l: left, r: 8, t: 10, b: 24 };
    return { W: W, H: height, m: m, pw: W - m.l - m.r, ph: height - m.t - m.b };
  }

  function axes(svgEl, f, yt, fmtY, times, range) {
    var g = s("g", { class: "axes" }, svgEl);
    yt.ticks.forEach(function (v) {
      var y = f.m.t + f.ph - (v / yt.top) * f.ph;
      s("line", { x1: f.m.l, x2: f.m.l + f.pw, y1: y, y2: y, class: v === 0 ? "baseline" : "grid" }, g);
      var t = s("text", { x: f.m.l - 6, y: y + 4, class: "tick", "text-anchor": "end" }, g);
      t.textContent = fmtY(v);
    });
    var every = TICK_EVERY[range] || 3600;
    var slot = f.pw / times.length;
    var lastX = -Infinity;
    times.forEach(function (ms, i) {
      var local = ms / 1000 - new Date(ms).getTimezoneOffset() * 60;
      if (Math.round(local) % every !== 0) return;
      var x = f.m.l + i * slot;
      if (x - lastX < 56 || x > f.m.l + f.pw - 18) return;
      lastX = x;
      var t = s("text", { x: x, y: f.H - 6, class: "tick", "text-anchor": "middle" }, g);
      t.textContent = xLabel(ms, range);
    });
  }

  /** Pointer + keyboard hover over the plot; calls show(i) / hide(). */
  function hoverLayer(svgEl, f, n, show, hide) {
    var slot = f.pw / n;
    var overlay = s("rect", { x: f.m.l, y: f.m.t, width: f.pw, height: f.ph, class: "hit" }, svgEl);
    var current = -1;
    function at(clientX) {
      var r = svgEl.getBoundingClientRect();
      var x = (clientX - r.left) * (f.W / r.width) - f.m.l;
      return Math.max(0, Math.min(n - 1, Math.floor(x / slot)));
    }
    overlay.addEventListener("pointermove", function (e) { current = at(e.clientX); show(current); });
    overlay.addEventListener("pointerdown", function (e) { current = at(e.clientX); show(current); });
    overlay.addEventListener("pointerleave", function () { hide(); });
    svgEl.setAttribute("tabindex", "0");
    svgEl.addEventListener("focus", function () { if (current < 0) current = n - 1; show(current); });
    svgEl.addEventListener("blur", hide);
    svgEl.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft") current = Math.max(0, current - 1);
      else if (e.key === "ArrowRight") current = Math.min(n - 1, current + 1);
      else if (e.key === "Escape") return hide();
      else return;
      e.preventDefault();
      show(current);
    });
  }

  function tooltip(container) {
    var tip = h("div", "chart-tip", null, container);
    tip.hidden = true;
    return {
      show: function (title, rows, xPx, svgW) {
        tip.replaceChildren();
        h("div", "tip-title", title, tip);
        rows.forEach(function (r) {
          var row = h("div", "tip-row", null, tip);
          var key = h("span", "tip-key " + (r.cls || ""), null, row);
          if (r.line) key.classList.add("is-line");
          h("strong", null, r.value, row);
          h("span", "tip-label", r.label, row);
        });
        tip.hidden = false;
        var scale = container.clientWidth / svgW;
        var x = xPx * scale;
        var w = tip.offsetWidth;
        var left = x + 14 + w > container.clientWidth ? x - 14 - w : x + 14;
        tip.style.left = Math.max(0, left) + "px";
      },
      hide: function () { tip.hidden = true; },
    };
  }

  function emptyText(svgEl, f, text) {
    var t = s("text", { x: f.m.l + f.pw / 2, y: f.m.t + f.ph / 2, class: "empty", "text-anchor": "middle" }, svgEl);
    t.textContent = text;
  }

  function newSvg(container, f, label) {
    container.replaceChildren();
    return s("svg", {
      viewBox: "0 0 " + f.W + " " + f.H, width: "100%", class: "chart-svg",
      role: "img", "aria-label": label,
    }, container);
  }

  // -- requests: stacked columns by status class ---------------------------

  function drawRequests(container, legendEl, d) {
    var o = d.overall, n = d.buckets.length;
    var times = d.buckets.map(Date.parse);
    var f = frame(container, 220, 44);
    var yt = niceTicks(Math.max.apply(null, o.hits), true);
    var svgEl = newSvg(container, f, "Requests per " + (d.step_s / 60) + " minutes by status class");
    axes(svgEl, f, yt, compact, times, d.range);

    var slot = f.pw / n;
    var gap = slot >= 6 ? 2 : slot >= 3 ? 1 : 0;
    var bw = Math.min(24, slot - gap);
    var segGap = bw >= 4 ? 1 : 0;
    var band = s("rect", { y: f.m.t, height: f.ph, width: slot, class: "hover-band", visibility: "hidden" }, svgEl);
    var bars = s("g", null, svgEl);
    var scale = f.ph / yt.top;
    var base = f.m.t + f.ph;

    for (var i = 0; i < n; i++) {
      var x = f.m.l + i * slot + (slot - bw) / 2;
      var y = base;
      var parts = STATUS.map(function (st) { return o[st.key][i]; });
      var topIdx = -1;
      parts.forEach(function (v, k) { if (v > 0) topIdx = k; });
      for (var k = 0; k <= topIdx; k++) {
        var v = parts[k];
        if (!v) continue;
        var hgt = v * scale;
        var isTop = k === topIdx;
        var drawH = isTop ? hgt : Math.max(0.5, hgt - segGap);
        var yTop = y - hgt;
        if (isTop && bw >= 4 && drawH >= 2) {
          var r = Math.min(3, bw / 2, drawH);
          s("path", {
            class: STATUS[k].cls,
            d: "M" + x + "," + (yTop + drawH) + "V" + (yTop + r) +
              "Q" + x + "," + yTop + " " + (x + r) + "," + yTop +
              "H" + (x + bw - r) + "Q" + (x + bw) + "," + yTop + " " + (x + bw) + "," + (yTop + r) +
              "V" + (yTop + drawH) + "Z",
          }, bars);
        } else {
          s("rect", { class: STATUS[k].cls, x: x, y: yTop, width: bw, height: drawH }, bars);
        }
        y = yTop;
      }
    }
    if (!d.totals.hits) emptyText(svgEl, f, "No requests in this range");

    var tip = tooltip(container);
    hoverLayer(svgEl, f, n, function (i) {
      band.setAttribute("x", f.m.l + i * slot);
      band.setAttribute("visibility", "visible");
      var rows = STATUS.slice().reverse().map(function (st) {
        return { cls: st.cls, value: fmtInt(o[st.key][i]), label: st.label };
      });
      rows.push({ cls: "tip-none", value: fmtInt(o.hits[i]), label: "total" });
      tip.show(bucketTitle(times[i], d.step_s), rows, f.m.l + (i + 0.5) * slot, f.W);
    }, function () { band.setAttribute("visibility", "hidden"); tip.hide(); });

    legendEl.replaceChildren();
    STATUS.forEach(function (st) {
      var item = h("span", "legend-item", null, legendEl);
      h("span", "legend-swatch " + st.cls, null, item);
      h("span", null, st.label + " ", item);
      h("strong", null, fmtInt(d.totals[st.key]), item);
    });
  }

  // -- latency: avg + max lines ---------------------------------------------

  function linePath(values, xAt, yAt) {
    var dStr = "", pen = false, singles = [];
    values.forEach(function (v, i) {
      if (v == null) { pen = false; return; }
      var prevNull = i === 0 || values[i - 1] == null;
      var nextNull = i === values.length - 1 || values[i + 1] == null;
      if (prevNull && nextNull) singles.push(i);
      dStr += (pen ? "L" : "M") + xAt(i).toFixed(1) + "," + yAt(v).toFixed(1);
      pen = true;
    });
    return { d: dStr, singles: singles };
  }

  function drawLatency(container, legendEl, d) {
    var o = d.overall, n = d.buckets.length;
    var times = d.buckets.map(Date.parse);
    var f = frame(container, 180, 52);
    var maxV = 0;
    o.max_ms.forEach(function (v) { if (v != null && v > maxV) maxV = v; });
    o.avg_ms.forEach(function (v) { if (v != null && v > maxV) maxV = v; });
    var yt = niceTicks(maxV, false);
    var svgEl = newSvg(container, f, "Average and maximum request latency");
    axes(svgEl, f, yt, function (v) { return v === 0 ? "0" : fmtMs(v); }, times, d.range);

    var slot = f.pw / n;
    var xAt = function (i) { return f.m.l + (i + 0.5) * slot; };
    var yAt = function (v) { return f.m.t + f.ph - (v / yt.top) * f.ph; };
    var cross = s("line", { y1: f.m.t, y2: f.m.t + f.ph, class: "crosshair", visibility: "hidden" }, svgEl);
    // Max first so the average line (the one most people read) sits on top.
    LATENCY.slice().reverse().forEach(function (ser) {
      var p = linePath(o[ser.key], xAt, yAt);
      if (p.d) s("path", { d: p.d, class: "line " + ser.cls }, svgEl);
      p.singles.forEach(function (i) {
        s("circle", { cx: xAt(i), cy: yAt(o[ser.key][i]), r: 4, class: "dot " + ser.cls }, svgEl);
      });
    });
    var markers = LATENCY.map(function (ser) {
      return s("circle", { r: 4, class: "dot " + ser.cls, visibility: "hidden" }, svgEl);
    });
    if (maxV === 0) emptyText(svgEl, f, "No timed requests in this range");

    var tip = tooltip(container);
    hoverLayer(svgEl, f, n, function (i) {
      var x = xAt(i);
      cross.setAttribute("x1", x); cross.setAttribute("x2", x);
      cross.setAttribute("visibility", "visible");
      LATENCY.forEach(function (ser, k) {
        var v = o[ser.key][i];
        if (v == null) { markers[k].setAttribute("visibility", "hidden"); return; }
        markers[k].setAttribute("cx", x); markers[k].setAttribute("cy", yAt(v));
        markers[k].setAttribute("visibility", "visible");
      });
      tip.show(bucketTitle(times[i], d.step_s), LATENCY.map(function (ser) {
        return { cls: ser.cls, line: true, value: fmtMs(o[ser.key][i]), label: ser.label };
      }), x, f.W);
    }, function () {
      cross.setAttribute("visibility", "hidden");
      markers.forEach(function (m) { m.setAttribute("visibility", "hidden"); });
      tip.hide();
    });

    legendEl.replaceChildren();
    LATENCY.forEach(function (ser) {
      var item = h("span", "legend-item", null, legendEl);
      h("span", "legend-line " + ser.cls, null, item);
      h("span", null, ser.label + " ", item);
      h("strong", null, fmtMs(d.totals[ser.key]), item);
    });
  }

  // -- per-service table ----------------------------------------------------

  function sparkline(values) {
    var W = 96, H = 24;
    var max = Math.max.apply(null, values);
    var svgEl = s("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, class: "sparkline", "aria-hidden": "true" });
    if (!(max > 0)) {
      s("line", { x1: 0, x2: W, y1: H - 1, y2: H - 1, class: "spark-flat" }, svgEl);
      return svgEl;
    }
    var step = W / Math.max(1, values.length - 1);
    var pts = values.map(function (v, i) {
      return (i * step).toFixed(1) + "," + (H - 1 - (v / max) * (H - 3)).toFixed(1);
    });
    s("path", { d: "M0," + (H - 1) + "L" + pts.join("L") + "L" + W + "," + (H - 1) + "Z", class: "spark-area" }, svgEl);
    s("polyline", { points: pts.join(" "), class: "spark-line" }, svgEl);
    return svgEl;
  }

  function drawTable(tbody, d, slug, onPick) {
    tbody.replaceChildren();
    if (!d.hosts.length) {
      var tr0 = h("tr", null, null, tbody);
      var td0 = h("td", "muted", "No proxy hosts yet — add or resync them on the ", tr0);
      td0.colSpan = 8;
      var a = h("a", null, "Proxy hosts", td0);
      a.href = "/proxy-hosts";
      td0.appendChild(document.createTextNode(" page."));
      return;
    }
    d.hosts.forEach(function (row) {
      var t = row.totals;
      var tr = h("tr", "host-row", null, tbody);
      if (row.service_slug) {
        tr.classList.add("is-clickable");
        tr.tabIndex = 0;
        tr.setAttribute("aria-pressed", String(row.service_slug === slug));
        if (row.service_slug === slug) tr.classList.add("is-selected");
        var pickRow = function () { onPick(row.service_slug === slug ? "" : row.service_slug, row.service_name); };
        tr.addEventListener("click", function (e) { if (!e.target.closest("a")) pickRow(); });
        tr.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickRow(); }
        });
      }
      var svc = h("td", null, null, tr);
      if (row.service_slug) {
        var link = h("a", null, row.service_name || row.service_slug, svc);
        link.href = "/services/" + encodeURIComponent(row.service_slug);
      } else {
        h("span", "muted", "unlinked", svc);
      }
      h("code", null, row.hostname, h("td", "host-cell", null, tr));
      h("td", "num", fmtInt(t.hits), tr);
      var c4 = h("td", "num", fmtInt(t.s4xx), tr);
      if (t.s4xx) h("span", "rate", " " + pct(t.s4xx, t.hits), c4);
      var c5 = h("td", "num", fmtInt(t.s5xx), tr);
      if (t.s5xx) h("span", "rate", " " + pct(t.s5xx, t.hits), c5);
      h("td", "num", fmtMs(t.avg_ms), tr);
      h("td", "num", fmtMs(t.max_ms), tr);
      var sp = h("td", "spark-cell", null, tr);
      var line = sparkline(row.hits);
      var title = s("title", null, line);
      title.textContent = fmtInt(t.hits) + " hits in range";
      sp.appendChild(line);
    });
  }

  // -- page wiring ----------------------------------------------------------

  var root = document.getElementById("traffic-root");
  if (!root) return;
  var tcRoot = document.querySelector("[data-tc]");
  var slug = "";
  var last = null;
  var tc = null;

  function setTiles(d) {
    var t = d.totals;
    var set = function (key, main, sub) {
      var el = root.querySelector('[data-stat="' + key + '"]');
      el.replaceChildren();
      el.appendChild(document.createTextNode(main));
      if (sub) h("span", "stat-sub", " " + sub, el);
    };
    set("hits", fmtInt(t.hits));
    set("s4xx", fmtInt(t.s4xx), pct(t.s4xx, t.hits));
    set("s5xx", fmtInt(t.s5xx), pct(t.s5xx, t.hits));
    set("avg", fmtMs(t.avg_ms), t.max_ms != null ? "max " + fmtMs(t.max_ms) : "");
  }

  function setChip(d) {
    var row = root.querySelector("[data-filter-row]");
    if (!slug) { row.hidden = true; return; }
    var match = d.hosts.filter(function (x) { return x.service_slug === slug; })[0];
    root.querySelector("[data-filter-name]").textContent = match ? (match.service_name || slug) : slug;
    row.hidden = false;
  }

  function renderCharts(d) {
    drawRequests(root.querySelector('[data-chart="requests"]'), root.querySelector('[data-legend="requests"]'), d);
    drawLatency(root.querySelector('[data-chart="latency"]'), root.querySelector('[data-legend="latency"]'), d);
  }

  function render(d) {
    setTiles(d);
    setChip(d);
    renderCharts(d);
    drawTable(root.querySelector("[data-host-rows]"), d, slug, pick);
  }

  function pick(newSlug) {
    slug = newSlug || "";
    tc.setParam("slug", slug);
    tc.refresh();
  }

  async function load(state) {
    var q = new URLSearchParams({ range: state.range });
    if (slug) q.set("slug", slug);
    var statusP = window.CollectorStatus ? window.CollectorStatus.refresh().catch(function () {}) : null;
    var r = await fetch("/api/traffic/series?" + q.toString(), { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error("series " + r.status);
    last = await r.json();
    render(last);
    await statusP;
  }

  tc = new window.TimeControls(tcRoot, { onRefresh: load });
  slug = tc.getParam("slug") || root.dataset.slug || "";
  root.querySelector("[data-filter-clear]").addEventListener("click", function () { pick(""); });
  window.addEventListener("resize", debounce(function () { if (last) renderCharts(last); }, 150));
  tc.start();
})();
