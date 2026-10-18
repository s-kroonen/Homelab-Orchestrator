/* /traffic/geo: the server renders the first paint; the time controls and
 * the service filter re-fetch GET /api/traffic/geo?range=&slug= and rebuild
 * #geo-body in place (same markup as traffic_geo.html). Untrusted strings
 * (IPs, country names, slugs) only go in via textContent. */
(function () {
  "use strict";

  var body = document.getElementById("geo-body");
  var tcRoot = document.querySelector("[data-tc]");
  if (!body || !tcRoot) return;
  var form = document.querySelector("[data-geo-filter]");
  var select = form && form.querySelector("select[name=slug]");
  var rangeInput = form && form.querySelector("input[name=range]");
  // What the server painted (it read ?range=, defaulting to 24h).
  var painted = (rangeInput && rangeInput.value) || "24h";

  function h(tag, cls, text, parent) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }
  function flag(code) {
    code = (code || "").trim().toUpperCase();
    if (!/^[A-Z]{2}$/.test(code)) return "";
    return String.fromCodePoint(0x1f1e6 + code.charCodeAt(0) - 65, 0x1f1e6 + code.charCodeAt(1) - 65);
  }
  function label(bucket, name, code) {
    if (bucket === "local") return "Local network";
    return name || code || "Unknown";
  }
  function round1(x) { return Math.round(x * 10) / 10; }

  function render(view, slug) {
    body.replaceChildren();
    if (!view.sample_count) {
      var n = h("div", "notice", null, body);
      var strong = h("strong", null, "No GeoIP samples in this range", n);
      if (slug) {
        strong.appendChild(document.createTextNode(" for "));
        h("code", null, slug, strong);
      }
      strong.appendChild(document.createTextNode("."));
      n.appendChild(document.createTextNode(
        " The collector tails Traefik's access log on each poll; rows appear here once " +
        "requests reach a proxy host linked to a service."));
      return;
    }
    var total = 0, local = 0, countryCount = 0;
    view.countries.forEach(function (c) {
      total += c.hits;
      if (c.bucket === "local") local += c.hits;
      if (c.bucket === "country") countryCount += 1;
    });
    var localPct = total ? round1((local / total) * 100) : 0;
    var extPct = total ? round1(((total - local) / total) * 100) : 0;

    var summary = h("p", "msg geo-summary", null, body);
    h("strong", null, String(total), summary);
    summary.appendChild(document.createTextNode(
      " hits · " + view.sample_count + " samples · " + countryCount + " " +
      (countryCount === 1 ? "country" : "countries") +
      " · local " + localPct + "% / external " + extPct + "%"));
    var split = h("div", "geo-split", null, body);
    split.title = "local " + local + " · external " + (total - local);
    if (local) h("span", "local", null, split).style.width = localPct + "%";
    if (total - local) h("span", "external", null, split).style.width = extPct + "%";

    var names = {};
    view.countries.forEach(function (c) { if (c.country_code) names[c.country_code] = c.country_name; });

    var sec = h("section", null, null, body);
    h("h2", null, "Countries", sec);
    var tbody = table(sec, [["", ""], ["Country", ""], ["Hits", "num"], ["Share", ""]]);
    view.countries.forEach(function (c) {
      var tr = h("tr", null, null, tbody);
      h("td", "flag", c.bucket === "local" ? "" : flag(c.country_code), tr);
      var name = label(c.bucket, c.country_name, c.country_code);
      var td = h("td", null, null, tr);
      if (c.bucket === "country") td.textContent = name; else h("span", "muted", name, td);
      h("td", "num", String(c.hits), tr);
      var share = h("td", "nowrap", null, tr);
      var p = total ? round1((c.hits / total) * 100) : 0;
      var bar = h("div", "pctbar", null, share);
      var fill = h("span", c.bucket === "local" ? "local" : "", null, bar);
      fill.style.width = p + "%";
      h("span", "pct", p + "%", share);
    });

    var sec2 = h("section", null, null, body);
    h("h2", null, "Top client IPs", sec2);
    var tbody2 = table(sec2, [["IP", ""], ["Country", ""], ["Hits", "num"]]);
    view.top_ips.forEach(function (i) {
      var tr = h("tr", null, null, tbody2);
      h("code", null, i.client_ip, h("td", "ip", null, tr));
      var td = h("td", "nowrap", null, tr);
      var f = i.bucket === "local" ? "" : flag(i.country_code);
      if (f) { h("span", "flag", f, td); td.appendChild(document.createTextNode(" ")); }
      var name = label(i.bucket, names[i.country_code] || "", i.country_code);
      if (i.bucket === "country") td.appendChild(document.createTextNode(name)); else h("span", "muted", name, td);
      h("td", "num", String(i.hits), tr);
    });
  }

  function table(parent, cols) {
    var wrap = h("div", "table-scroll", null, parent);
    var t = h("table", null, null, wrap);
    var tr = h("tr", null, null, h("thead", null, null, t));
    cols.forEach(function (c) { h("th", c[1], c[0], tr); });
    return h("tbody", null, null, t);
  }

  var tc = new window.TimeControls(tcRoot, {
    onRefresh: async function (state) {
      var slug = select ? select.value : "";
      if (rangeInput) rangeInput.value = state.range;
      var q = new URLSearchParams({ range: state.range });
      if (slug) q.set("slug", slug);
      var statusP = window.CollectorStatus ? window.CollectorStatus.refresh().catch(function () {}) : null;
      var r = await fetch("/api/traffic/geo?" + q.toString(), { headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error("geo " + r.status);
      render(await r.json(), slug);
      await statusP;
    },
  });

  if (select) {
    select.addEventListener("change", function () {
      tc.setParam("slug", select.value);
      tc.refresh();
    });
  }
  if (form) form.addEventListener("submit", function (e) { e.preventDefault(); tc.refresh(); });

  // The server already painted the range from the URL; only re-fetch now if
  // the controls resolved to a different range (e.g. from localStorage).
  if (tc.state.range !== painted) {
    tc.start();
  } else {
    tc.lastUpdated = Date.now();
    tc.tick();
    tc.schedule();
  }
})();
