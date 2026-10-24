/* The map page: boxes view (nodes -> guests -> services, with the lines that show
 * where traffic and backups go) and a plain tree view of the same data.
 * Everything is built with textContent, never innerHTML. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const root = $("map-root");
  const state = {
    data: null, view: "boxes", q: "", problems: false, edges: new Set(), selected: null,
    groupBy: "box", sortBy: "name",
  };
  try {
    state.groupBy = localStorage.getItem("orch:map:groupBy") || state.groupBy;
    state.sortBy = localStorage.getItem("orch:map:sortBy") || state.sortBy;
  } catch (_e) { /* storage may be unavailable */ }
  for (const c of document.querySelectorAll("#edge-toggles input")) if (c.checked) state.edges.add(c.dataset.edge);

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  const badge = (verdict) => el("span", "badge " + (verdict === "parked" ? "disabled" : verdict), verdict);
  const isBad = (v) => v === "failed" || v === "down";

  function flatten(svc) { return [svc, ...svc.children.flatMap(flatten)]; }

  // -- filtering --------------------------------------------------------------
  function matches(svc) {
    const q = state.q;
    if (!q) return true;
    const hay = [svc.slug, svc.name, svc.ip, svc.group, ...svc.hosts.map((h) => h.hostname)].join(" ").toLowerCase();
    return hay.includes(q);
  }
  function subtreeMatches(svc, pred) {
    return pred(svc) || svc.children.some((c) => subtreeMatches(c, pred));
  }
  const showSvc = (svc) =>
    (!state.q || subtreeMatches(svc, matches)) && (!state.problems || subtreeMatches(svc, (s) => isBad(s.verdict)));

  function allServices(data) {
    const out = [];
    const walk = (s) => { out.push(s); s.children.forEach(walk); };
    for (const n of data.nodes) {
      n.loose.forEach(walk);
      n.guests.forEach((g) => g.services.forEach(walk));
    }
    return out;
  }

  // -- boxes view -------------------------------------------------------------
  function card(svc) {
    const c = el("div", "card " + svc.verdict);
    c.dataset.id = svc.id;
    c.dataset.slug = svc.slug;
    if (state.q && !matches(svc)) c.classList.add("dim");
    if (state.selected === svc.slug) c.classList.add("sel");
    const row = el("div", "row1");
    row.append(el("span", "nm", svc.name || svc.slug), badge(svc.verdict));
    if (svc.kind === "sub") row.append(el("span", "chip", "sub"));
    if (svc.controller) row.append(el("span", "chip", svc.controller));
    if (svc.recovery) row.append(el("span", "chip", "auto-recover"));
    if (svc.backup_gate === "required") row.append(el("span", "chip", "backup gate"));
    c.append(row);
    const meta = [svc.slug !== svc.name ? svc.slug : "", svc.ip, svc.group].filter(Boolean).join(" · ");
    if (meta) c.append(el("div", "meta", meta));
    if (svc.verdict === "failed" && svc.reason) c.append(el("div", "meta", svc.reason.slice(0, 140)));
    c.addEventListener("click", (ev) => { ev.stopPropagation(); openDrawer(svc.slug); });
    if (svc.children.length) {
      const kids = el("div", "kids");
      for (const k of svc.children.slice().sort(comparator())) if (showSvc(k)) kids.append(card(k));
      c.append(kids);
    }
    return c;
  }

  function nodeBox(n) {
    const box = el("div", "box" + (n.power === "off" ? " node-off" : ""));
    box.dataset.id = n.id;
    const h = el("div", "box-h");
    const pw = badge(n.power === "on" ? "healthy" : n.power === "off" ? "down" : "unknown");
    pw.textContent = "power " + n.power;
    h.append(el("span", "t", n.name), pw);
    if (n.always_on) h.append(el("span", "chip", "always on"));
    if (n.address) h.append(el("span", "s", n.address));
    box.append(h);
    for (const g of n.guests) {
      const svcs = g.services.filter(showSvc);
      if (!svcs.length && (state.q || state.problems)) continue;
      const gb = el("div", "guest");
      gb.dataset.id = g.id;
      const gh = el("div", "box-h");
      gh.append(el("span", "t", `${g.kind} ${g.vmid}`), el("span", "chip", g.status));
      gb.append(gh);
      for (const s of svcs.slice().sort(comparator())) gb.append(card(s));
      box.append(gb);
    }
    const loose = n.loose.filter(showSvc);
    if (loose.length) {
      const lb = el("div", "guest");
      const lh = el("div", "box-h");
      lh.append(el("span", "t", "no guest"));
      lb.append(lh);
      for (const s of loose) lb.append(card(s));
      box.append(lb);
    }
    return box;
  }

  // -- ordering ---------------------------------------------------------------
  const RANK = { failed: 0, down: 1, unknown: 2, parked: 3, disabled: 4, healthy: 5 };
  function ipKey(ip) {
    const m = /^(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(ip || "");
    return m ? ((+m[1] * 256 + +m[2]) * 256 + +m[3]) * 256 + +m[4] : Infinity;
  }
  function byName(a, b) { return (a.name || a.slug).localeCompare(b.name || b.slug, undefined, { numeric: true }); }
  function comparator() {
    switch (state.sortBy) {
      case "ip": return (a, b) => ipKey(a.ip) - ipKey(b.ip) || byName(a, b);
      case "status": return (a, b) => (RANK[a.verdict] ?? 9) - (RANK[b.verdict] ?? 9) || byName(a, b);
      case "group": return (a, b) => (a.group || "\uffff").localeCompare(b.group || "\uffff") || byName(a, b);
      default: return byName;
    }
  }
  const subnet = (ip) => (/^\d+\.\d+\.\d+\./.test(ip || "") ? ip.split(".").slice(0, 3).join(".") + ".x" : "");

  // Every root service with where it lives, for the layouts that are not "by box".
  function roots(data) {
    const out = [];
    for (const n of data.nodes) {
      for (const g of n.guests) for (const s of g.services) out.push({ svc: s, node: n.name, guest: `${g.kind} ${g.vmid}` });
      for (const s of n.loose) out.push({ svc: s, node: n.name, guest: "" });
    }
    return out;
  }

  // One box per group / subnet, services (with their sub-services nested) inside.
  function sectionBoxes(data) {
    const sections = new Map();
    for (const r of roots(data)) {
      if (!showSvc(r.svc)) continue;
      let key, title;
      if (state.groupBy === "group") { key = r.svc.group || ""; title = key || "no group"; }
      else { key = subnet(r.svc.ip); title = key || "no address"; }
      if (!sections.has(key)) sections.set(key, { title, key, items: [] });
      sections.get(key).items.push(r);
    }
    const ordered = [...sections.values()].sort((a, b) =>
      (a.key === "" ? 1 : 0) - (b.key === "" ? 1 : 0) ||
      (state.groupBy === "ip" ? ipKey(a.key.replace(/x$/, "0")) - ipKey(b.key.replace(/x$/, "0")) : a.title.localeCompare(b.title)));
    const cmp = comparator();
    return ordered.map((sec) => {
      const box = el("div", "box");
      const h = el("div", "box-h");
      const bad = sec.items.filter((r) => isBad(r.svc.verdict)).length;
      h.append(el("span", "t", sec.title), el("span", "s", `${sec.items.length} service(s)`));
      if (bad) { const b = badge("failed"); b.textContent = `${bad} problem(s)`; h.append(b); }
      box.append(h);
      for (const r of sec.items.sort((a, b) => cmp(a.svc, b.svc))) {
        const c = card(r.svc);
        const where = el("div", "meta", `on ${r.node}${r.guest ? " · " + r.guest : ""}`);
        c.insertBefore(where, c.querySelector(".kids"));
        box.append(c);
      }
      return box;
    });
  }

  function renderBoxes(data) {
    const cols = el("div", "cols");
    const entry = el("div");
    entry.append(el("h3", "col-h", "Entry · " + data.entry.name));
    const slugGroup = new Map(roots(data).concat([]).flatMap((r) => flatten(r.svc)).map((s) => [s.slug, s]));
    const hosts = [...data.entry.hosts].sort((a, b) => {
      const sa = slugGroup.get(a.service), sb = slugGroup.get(b.service);
      if (state.groupBy === "group") return ((sa && sa.group) || "\uffff").localeCompare((sb && sb.group) || "\uffff") || a.hostname.localeCompare(b.hostname);
      if (state.groupBy === "ip" || state.sortBy === "ip") return ipKey(sa && sa.ip) - ipKey(sb && sb.ip) || a.hostname.localeCompare(b.hostname);
      return a.hostname.localeCompare(b.hostname);
    });
    for (const h of hosts) {
      if (state.q && !h.hostname.toLowerCase().includes(state.q) && !(h.service || "").includes(state.q)) continue;
      if (state.problems) continue;
      const d = el("div", "host" + (h.served ? "" : " off"), h.hostname);
      d.dataset.id = h.id;
      if (!h.service) d.append(el("span", "chip", " no service"));
      entry.append(d);
    }
    const mid = el("div");
    mid.append(el("h3", "col-h", { box: "Boxes", group: "Groups", ip: "Addresses" }[state.groupBy] || "Boxes"));
    if (state.groupBy === "box") {
      for (const n of data.nodes) {
        const box = nodeBox(n);
        if ((state.q || state.problems) && !box.querySelector(".card")) continue;
        mid.append(box);
      }
    } else {
      mid.append(...sectionBoxes(data));
    }
    const pbs = el("div");
    pbs.append(el("h3", "col-h", "Backups"));
    const pb = el("div", "box pbs");
    pb.dataset.id = data.backup.id;
    const ph = el("div", "box-h");
    ph.append(el("span", "t", "Proxmox Backup Server"));
    pb.append(ph);
    pb.append(el("div", "meta", `datastore ${data.backup.datastore || "?"} · storage ${data.backup.storage || "?"}`));
    pb.append(el("div", "meta", `${data.backup.targets.length} guest(s) in scope`));
    pbs.append(pb);
    cols.append(entry, mid, pbs);
    root.replaceChildren(cols);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "edges");
    root.prepend(svg);
    drawEdges();
    wireHover();
  }

  // -- edges ------------------------------------------------------------------
  function findEl(id) {
    // a hidden (filtered) service falls back to its nearest visible ancestor card
    return root.querySelector(`[data-id="${CSS.escape(id)}"]`);
  }
  function drawEdges() {
    const svg = root.querySelector("svg.edges");
    if (!svg || !state.data) return;
    svg.replaceChildren();
    const base = root.getBoundingClientRect();
    const pt = (r, side) => ({
      x: (side === "r" ? r.right : r.left) - base.left,
      y: r.top + r.height / 2 - base.top,
    });
    for (const e of state.data.edges) {
      if (!state.edges.has(e.kind)) continue;
      const a = findEl(e.from), b = findEl(e.to);
      if (!a || !b) continue;
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      if (!ra.width || !rb.width) continue;
      let p1, p2, c1, c2;
      const dx = (ra.left + ra.width / 2) - (rb.left + rb.width / 2);
      if (Math.abs(dx) < 24) {
        // same column: loop out to the right
        p1 = pt(ra, "r"); p2 = pt(rb, "r");
        const bulge = 36 + Math.min(60, Math.abs(p1.y - p2.y) / 6);
        c1 = { x: p1.x + bulge, y: p1.y }; c2 = { x: p2.x + bulge, y: p2.y };
      } else if (dx < 0) {
        p1 = pt(ra, "r"); p2 = pt(rb, "l");
        const k = Math.max(30, (p2.x - p1.x) / 2);
        c1 = { x: p1.x + k, y: p1.y }; c2 = { x: p2.x - k, y: p2.y };
      } else {
        p1 = pt(ra, "l"); p2 = pt(rb, "r");
        const k = Math.max(30, (p1.x - p2.x) / 2);
        c1 = { x: p1.x - k, y: p1.y }; c2 = { x: p2.x + k, y: p2.y };
      }
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M${p1.x},${p1.y} C${c1.x},${c1.y} ${c2.x},${c2.y} ${p2.x},${p2.y}`);
      path.setAttribute("class", e.kind + (e.off ? " off" : "") + (e.stale ? " stale" : ""));
      path.dataset.from = e.from; path.dataset.to = e.to;
      const t = document.createElementNS("http://www.w3.org/2000/svg", "title");
      t.textContent = `${e.from} → ${e.to}${e.label ? " (" + e.label + ")" : ""}`;
      path.append(t);
      svg.append(path);
    }
  }
  function wireHover() {
    const svg = root.querySelector("svg.edges");
    if (!svg) return;
    for (const node of root.querySelectorAll("[data-id]")) {
      node.addEventListener("mouseenter", () => {
        const id = node.dataset.id;
        let any = false;
        for (const p of svg.querySelectorAll("path")) {
          const hit = p.dataset.from === id || p.dataset.to === id;
          p.classList.toggle("hl", hit);
          any = any || hit;
        }
        svg.classList.toggle("focus", any);
      });
      node.addEventListener("mouseleave", () => {
        svg.classList.remove("focus");
        for (const p of svg.querySelectorAll("path")) p.classList.remove("hl");
      });
    }
  }

  // -- tree view --------------------------------------------------------------
  function treeSvc(svc) {
    const li = el("li");
    li.append(el("span", "lvl", svc.kind));
    const a = el("a", "pick", svc.name || svc.slug);
    a.addEventListener("click", () => openDrawer(svc.slug));
    li.append(a, document.createTextNode(" "), badge(svc.verdict));
    const bits = [svc.ip, ...svc.hosts.map((h) => h.hostname + (h.served ? "" : " (off)"))].filter(Boolean);
    if (bits.length) li.append(el("span", "meta", "  " + bits.join(" · ")));
    const kids = svc.children.filter(showSvc).sort(comparator());
    if (kids.length) {
      const ul = el("ul");
      kids.forEach((k) => ul.append(treeSvc(k)));
      li.append(ul);
    }
    return li;
  }
  function renderTreeSections(data) {
    const wrap = el("div", "tree");
    const ul = el("ul");
    const by = new Map();
    for (const r of roots(data)) {
      if (!showSvc(r.svc)) continue;
      const key = state.groupBy === "group" ? (r.svc.group || "") : subnet(r.svc.ip);
      if (!by.has(key)) by.set(key, []);
      by.get(key).push(r);
    }
    const cmp = comparator();
    const keys = [...by.keys()].sort((a, b) => (a === "" ? 1 : 0) - (b === "" ? 1 : 0) || a.localeCompare(b, undefined, { numeric: true }));
    for (const key of keys) {
      const li = el("li");
      li.append(el("span", "lvl", state.groupBy === "group" ? "group" : "subnet"),
        el("strong", "", key || (state.groupBy === "group" ? "no group" : "no address")));
      const inner = el("ul");
      for (const r of by.get(key).sort((a, b) => cmp(a.svc, b.svc))) {
        const item = treeSvc(r.svc);
        item.insertBefore(el("span", "meta", ` on ${r.node}${r.guest ? " · " + r.guest : ""} `), item.querySelector("ul"));
        inner.append(item);
      }
      li.append(inner);
      ul.append(li);
    }
    wrap.append(ul);
    root.replaceChildren(wrap);
  }

  function renderTree(data) {
    if (state.groupBy !== "box") { renderTreeSections(data); return; }
    const wrap = el("div", "tree");
    const ul = el("ul");
    for (const n of data.nodes) {
      const li = el("li");
      li.append(el("span", "lvl", "node"), el("strong", "", n.name), document.createTextNode(" "),
        el("span", "chip", "power " + n.power));
      const inner = el("ul");
      for (const g of n.guests) {
        const svcs = g.services.filter(showSvc).sort(comparator());
        if (!svcs.length && (state.q || state.problems)) continue;
        const gl = el("li");
        gl.append(el("span", "lvl", "guest"), el("strong", "", `${g.kind} ${g.vmid}`), document.createTextNode(" "),
          el("span", "chip", g.status));
        const gu = el("ul");
        svcs.forEach((s) => gu.append(treeSvc(s)));
        gl.append(gu);
        inner.append(gl);
      }
      n.loose.filter(showSvc).forEach((s) => inner.append(treeSvc(s)));
      if (!inner.children.length && (state.q || state.problems)) continue;
      li.append(inner);
      ul.append(li);
    }
    wrap.append(ul);
    root.replaceChildren(wrap);
  }

  // -- drawer -----------------------------------------------------------------
  function findSvc(slug) { return state.data ? allServices(state.data).find((s) => s.slug === slug) : null; }
  function openDrawer(slug) {
    const svc = findSvc(slug);
    if (!svc) return;
    state.selected = slug;
    const body = $("drawer-body");
    body.replaceChildren();
    const h = el("h3", "", svc.name || svc.slug);
    body.append(h, badge(svc.verdict));
    if (svc.reason) body.append(el("p", "msg", svc.reason));
    const dl = el("dl");
    const add = (k, v) => { if (v === "" || v === null || v === undefined) return; dl.append(el("dt", "", k), el("dd", "", String(v))); };
    add("Slug", svc.slug);
    add("Kind", svc.kind);
    add("IP", svc.ip);
    add("Group", svc.group);
    add("Controller", svc.controller || "(the guest itself)");
    add("Probes", `${svc.probes.total} (${svc.probes.required} required)`);
    add("Hosts", svc.hosts.map((x) => x.hostname + (x.served ? "" : " (off)")).join(", "));
    add("Backup policy", svc.policy);
    add("Backup gate", svc.backup_gate);
    add("Last backup", svc.last_backup ? new Date(svc.last_backup.at).toLocaleString() + (svc.last_backup.verified ? " ✓" : "") : "");
    add("Auto-recovery", svc.recovery ? "on" : "");
    add("Sub-services", svc.children.map((c) => c.slug).join(", "));
    body.append(dl);
    const acts = el("div", "acts");
    for (const action of ["start", "stop", "restart"]) {
      const b = el("button", action === "start" ? "" : "secondary", action[0].toUpperCase() + action.slice(1));
      b.type = "button";
      b.addEventListener("click", async () => {
        if (action !== "start" && !confirm(`${action} ${svc.slug}?`)) return;
        b.disabled = true;
        try {
          const r = await apiFetch("POST", `/api/services/${encodeURIComponent(svc.slug)}/actions/${action}`, { target: "service" });
          toast(r.message || `${action} ok`, "ok");
          await load();
        } catch (e) { /* apiFetch already toasted */ } finally { b.disabled = false; }
      });
      acts.append(b);
    }
    const link = el("a", "", "Open service page →");
    link.href = `/services/${encodeURIComponent(svc.slug)}`;
    acts.append(link);
    body.append(acts);
    $("drawer").hidden = false;
    for (const c of root.querySelectorAll(".card.sel")) c.classList.remove("sel");
    const cardEl = root.querySelector(`.card[data-slug="${CSS.escape(slug)}"]`);
    if (cardEl) cardEl.classList.add("sel");
  }
  function closeDrawer() {
    state.selected = null;
    $("drawer").hidden = true;
    for (const c of root.querySelectorAll(".card.sel")) c.classList.remove("sel");
  }

  // -- plumbing ---------------------------------------------------------------
  function render() {
    if (!state.data) return;
    if (state.view === "tree") renderTree(state.data);
    else renderBoxes(state.data);
    const c = state.data.counts;
    $("m-msg").textContent = `${c.nodes} nodes · ${c.services} services · ${c.sub_services} sub-services · ${c.hosts} hosts`;
  }
  async function load() {
    try {
      state.data = await apiFetch("GET", "/api/map", undefined, { suppressToast: true });
      render();
      if (state.selected) openDrawer(state.selected);
    } catch (e) {
      $("m-msg").textContent = "Could not load the map: " + e.message;
    }
  }

  for (const b of document.querySelectorAll("#view-seg button")) {
    b.addEventListener("click", () => {
      state.view = b.dataset.view;
      for (const o of document.querySelectorAll("#view-seg button")) o.classList.toggle("on", o === b);
      document.getElementById("edge-toggles").style.display = state.view === "boxes" ? "" : "none";
      render();
    });
  }
  for (const [id, key] of [["m-group", "groupBy"], ["m-sort", "sortBy"]]) {
    $(id).value = state[key];
    $(id).addEventListener("change", (e) => {
      state[key] = e.target.value;
      try { localStorage.setItem("orch:map:" + key, state[key]); } catch (_e) { /* ignore */ }
      render();
    });
  }
  $("m-search").addEventListener("input", (e) => { state.q = e.target.value.trim().toLowerCase(); render(); });
  $("m-problems").addEventListener("change", (e) => { state.problems = e.target.checked; render(); });
  for (const c of document.querySelectorAll("#edge-toggles input")) {
    c.addEventListener("change", () => {
      if (c.checked) state.edges.add(c.dataset.edge); else state.edges.delete(c.dataset.edge);
      drawEdges(); wireHover();
    });
  }
  $("m-refresh").addEventListener("click", load);
  $("drawer-close").addEventListener("click", closeDrawer);
  window.addEventListener("resize", () => { if (state.view === "boxes") { drawEdges(); wireHover(); } });
  setInterval(() => { if (!document.hidden) load(); }, 30000);
  load();
})();
