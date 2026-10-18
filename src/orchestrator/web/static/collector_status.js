/* Re-renders the access-log collector status (templates/_collector_status.html)
 * from GET /api/traffic/collector-status's `view`, so the strip stays live
 * when the time controls refresh. Same markup as the server's first paint;
 * every untrusted string goes in via textContent. */
(function () {
  "use strict";

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function ago(iso) {
    var t = Date.parse(iso);
    var fn = window.TimeControls && window.TimeControls.agoText;
    return isNaN(t) || !fn ? "" : fn(Date.now() - t);
  }

  function timeEl(iso) {
    var t = el("time", null, ago(iso));
    t.setAttribute("datetime", iso);
    t.setAttribute("data-age", "");
    return t;
  }

  function render(container, v) {
    container.setAttribute("data-level", v.level);
    container.replaceChildren();
    var unmatchedHits = (v.unmatched || []).reduce(function (a, u) { return a + u.hits; }, 0);

    if (v.level === "ok") {
      var strip = el("p", "status-strip");
      var dot = el("span", "status-dot");
      dot.setAttribute("aria-hidden", "true");
      strip.appendChild(dot);
      var last = el("span", null, "Last poll ");
      last.appendChild(timeEl(v.last_poll_at));
      strip.appendChild(last);
      strip.appendChild(el("span", null, v.lines + " lines"));
      strip.appendChild(el("span", null, v.matched + " matched"));
      if (unmatchedHits) {
        var um = el("span", "muted", unmatchedHits + " unmatched");
        um.title = v.unmatched.map(function (u) { return u.router + " (" + u.hits + ")"; }).join(", ");
        strip.appendChild(um);
      }
      container.appendChild(strip);
      return;
    }

    var box = el("div", "notice notice-" + v.level);
    box.setAttribute("role", "status");
    box.appendChild(el("strong", null, v.headline));
    if (v.error) box.appendChild(el("blockquote", "status-error", v.error));
    var listUnmatched = v.unmatched && v.unmatched.length && v.matched === 0;
    if (listUnmatched) {
      var ul = el("ul", "unmatched-list");
      v.unmatched.forEach(function (u) {
        var li = el("li");
        li.appendChild(el("code", null, u.router));
        li.appendChild(document.createTextNode(" "));
        li.appendChild(el("span", "muted", u.hits + (u.hits === 1 ? " hit" : " hits")));
        ul.appendChild(li);
      });
      box.appendChild(ul);
    }
    if (v.hint) {
      var p = el("p", null, v.hint);
      if (listUnmatched) {
        p.appendChild(document.createTextNode(" "));
        var a = el("a", null, "Proxy hosts → Resync");
        a.href = "/proxy-hosts";
        p.appendChild(a);
      }
      box.appendChild(p);
    }
    if (v.last_poll_at) {
      var meta = el("p", "status-meta", "Last poll ");
      meta.appendChild(timeEl(v.last_poll_at));
      meta.appendChild(document.createTextNode(" · " + v.lines + " lines · " + v.matched + " matched"));
      box.appendChild(meta);
    }
    container.appendChild(box);
  }

  async function refresh() {
    var container = document.getElementById("collector-status");
    if (!container) return;
    var r = await fetch("/api/traffic/collector-status", { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error("collector-status " + r.status);
    var body = await r.json();
    render(container, body.view);
  }

  window.CollectorStatus = { render: render, refresh: refresh };
})();
