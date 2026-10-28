/* The map (templates/map.html), to the Map wireframe: one row per node with the proxy hosts that
 * lead to it on the left, the backup server every node shares on the right, and a selection
 * sidebar that says what the selected node, service or host is, why, what to do and its path.
 * Lines are drawn for what is selected, or for everything.
 * Data: GET /api/views/map (the database, at once); then the live reads: node power
 * (/api/infra/nodes/status) and the datastore (/api/infra/pbs/datastore). Refreshed in place. */
(function () {
  "use strict";
  const { $, $$, el, clear, fill, api, fmt, region, menuButton } = window.UI;
  const O = window.Objects;
  const enc = encodeURIComponent;
  const SVG = "http://www.w3.org/2000/svg";
  const root = $("#map-root");
  const side = $("#map-side");
  const sideBody = $("#map-side-body");
  const params = new URLSearchParams(location.search);
  let V = JSON.parse($("#initial").textContent);
  let live = null;        // /api/infra/nodes/status
  let store = null;       // /api/infra/pbs/datastore
  let sel = params.get("sel") || "";
  let q = (params.get("q") || "").trim().toLowerCase();
  let problems = params.get("problems") === "1";
  let lines = "selected";
  try { lines = localStorage.getItem("orch:map:lines") || lines; } catch (_e) { /* storage may be off */ }
  let I = null;           // the index of the current data (see index())

  // --- the data, indexed ------------------------------------------------------------------------
  function index() {
    const rows = new Map(V.view.rows.map((r) => [r.slug, r]));
    const nodes = new Map(V.nodes.map((n) => [n.name, n]));
    const svcs = new Map();      // slug -> {svc, root, node, guest}
    const roots = [];            // {svc, node, guest} in the order they are drawn
    const walk = (s, rootSlug, node, guest) => {
      svcs.set(s.slug, { svc: s, root: rootSlug, node, guest });
      s.children.forEach((c) => walk(c, rootSlug, node, guest));
    };
    const place = (s, node, guest) => { roots.push({ svc: s, node, guest }); walk(s, s.slug, node, guest); };
    for (const n of V.map.nodes) {
      n.guests.forEach((g) => g.services.forEach((s) => place(s, n.name, g)));
      n.loose.forEach((s) => place(s, n.name, null));
    }
    (V.map.unplaced || []).forEach((s) => place(s, null, null));
    const hostsOf = new Map();   // root slug -> its hosts and its sub-services' hosts
    const loose = [];
    for (const h of V.map.entry.hosts) {
      const t = h.service && svcs.get(h.service);
      if (!t) { loose.push(h); continue; }
      if (!hostsOf.has(t.root)) hostsOf.set(t.root, []);
      hostsOf.get(t.root).push(h);
    }
    const policies = new Map(V.policies.map((p) => [p.name, p]));
    return { rows, nodes, svcs, roots, hostsOf, loose, policies };
  }

  const row = (slug) => I.rows.get(slug) || { slug, name: slug, state: { cls: "off", label: "unknown", why: "" }, backup: {}, enabled: true };
  const nodeOf = (name) => I.nodes.get(name) || null;
  const canWake = (n) => !!n && !n.always_on && !!n.power_mgr_target;
  function power(name) {
    const l = live && live.nodes.find((x) => x.name === name);
    if (l && l.state) return l.state;
    const m = V.map.nodes.find((x) => x.name === name);
    return m ? m.power : "unknown";
  }
  const powerCls = (p) => (p === "on" ? "ok" : p === "off" || p === "unknown" ? "off" : "pending");
  const guestLabel = (g) => (g ? (g.kind === "lxc" ? "CT " : "VM ") + g.vmid : "");
  const pill = (cls, text, title) => el("span", { class: "pill " + cls, title: title || null }, text);

  // --- filters ---------------------------------------------------------------------------------
  function matches(t) {
    const r = row(t.svc.slug);
    if (problems && r.state.cls !== "fail" && r.state.cls !== "warn") return false;
    if (!q) return true;
    const hosts = I.hostsOf.get(t.svc.slug) || [];
    const hay = [t.svc.slug, t.svc.name, t.svc.ip, t.svc.group, t.node, guestLabel(t.guest), ...hosts.map((h) => h.hostname)];
    const kids = []; const walk = (s) => { kids.push(s.slug, s.name, s.ip); s.children.forEach(walk); }; t.svc.children.forEach(walk);
    return hay.concat(kids).join(" ").toLowerCase().includes(q);
  }

  // --- drawing ----------------------------------------------------------------------------------
  function hostGroup(t, hosts) {
    const r = row(t.svc.slug);
    return el("div", { class: "map-hg" },
      el("div", { class: "map-hg-h", "data-id": "svc-hg:" + t.svc.slug, onclick: () => select("svc:" + t.svc.slug) },
        el("span", { class: "dot " + r.state.cls }), el("span", { class: "nm" }, r.name), el("span", { class: "n" }, String(hosts.length))),
      hosts.map((h) => el("button", { type: "button", class: "map-host" + (h.served ? "" : " off"), "data-id": h.id, onclick: () => select(h.id) },
        el("span", { class: "mono" }, h.hostname), h.served ? null : el("span", { class: "faint" }, " · off"))));
  }

  function svcRow(t) {
    const r = row(t.svc.slug);
    const kids = t.svc.children.length;
    return el("button", { type: "button", class: "map-svc", "data-id": "svc:" + t.svc.slug, onclick: () => select("svc:" + t.svc.slug) },
      el("span", { class: "vm mono" }, t.guest ? guestLabel(t.guest) : "—"),
      el("span", { class: "nm" }, r.name, kids ? el("span", { class: "kids", title: kids + " sub-service" + (kids === 1 ? "" : "s") }, " +" + kids) : null),
      pill(r.state.cls, r.state.label, r.state.why));
  }

  function nodeCard(n, list) {
    const node = nodeOf(n.name);
    const p = power(n.name);
    const l = live && live.nodes.find((x) => x.name === n.name);
    const h = l && l.health;
    const bits = [node && node.always_on ? "always on" : canWake(node) ? "wakes on demand" : "no power target",
      n.address, h && h.reachable && h.version ? "Proxmox " + String(h.version).replace(/^pve-manager\//, "").split("/")[0] : ""].filter(Boolean).join(" · ");
    const btn = p === "off" && canWake(node)
      ? el("button", { type: "button", class: "small", onclick: (e) => { e.stopPropagation(); O.nodePower(node, "wake", refreshAll); } }, "Wake")
      : el("button", { type: "button", class: "secondary small", onclick: (e) => { e.stopPropagation(); nodeStatus.refresh(); } }, "Check");
    return el("div", { class: "map-node" + (p === "on" ? "" : " is-off"), "data-id": "node:" + n.name },
      el("div", { class: "map-node-h", onclick: () => select("node:" + n.name) },
        el("strong", {}, n.name), pill(powerCls(p), p), el("span", { style: { flex: 1 } }), btn),
      el("div", { class: "map-node-sub", onclick: () => select("node:" + n.name) }, bits),
      list.length ? list.map(svcRow)
        : (q || problems) ? el("p", { class: "faint map-none" }, "Nothing here matches.")
          : el("p", { class: "faint map-none" }, "No services on this node yet. ", el("a", { href: "/nodes?q=" + enc(n.name) }, "Import its guests")));
  }

  function backupsCard(rowsCount) {
    const b = V.map.backup;
    const used = sized() ? store.used_fraction : null;
    const perNode = new Map();
    for (const t of b.targets) {
      const s = I.svcs.get(t.slug);
      const k = s && s.node ? s.node : "";
      if (!perNode.has(k)) perNode.set(k, []);
      perNode.get(k).push(t);
    }
    const nodeRow = (name, targets) => {
      const pols = [...new Set(targets.map((t) => t.policy).filter(Boolean))];
      const nxt = pols.map((p) => I.policies.get(p)).filter((p) => p && p.next_run).sort((a, c) => a.next_run.localeCompare(c.next_run))[0];
      const node = nodeOf(name);
      const restores = pols.some((p) => (I.policies.get(p) || {}).restore_power_state);
      const sub = !targets.length ? "nothing yet"
        : [pols.length ? pols[0] + (pols.length > 1 ? " +" + (pols.length - 1) : "") : "no policy",
          nxt ? "next " + fmt.short(nxt.next_run) : "", canWake(node) ? (restores ? "wakes it, puts it back off" : "wakes it") : ""].filter(Boolean).join(" · ");
      return el("button", { type: "button", class: "map-bnode", "data-id": "pbsnode:" + name, onclick: () => select(name ? "node:" + name : "pbs") },
        el("span", { class: "nm" }, name || "no node"), el("span", { class: "n" }, targets.length ? String(targets.length) : "—"),
        el("span", { class: "sub" }, sub));
    };
    const names = V.map.nodes.map((n) => n.name);
    const card = el("div", { class: "map-pbs", "data-id": "pbs" },
      el("button", { type: "button", class: "map-pbs-h", onclick: () => select("pbs") }, el("strong", {}, "PBS · " + (b.datastore || "datastore"))),
      el("div", { class: "bar" }, el("span", { class: used != null && used > 0.85 ? "warn" : "", style: { width: (used != null ? Math.round(used * 100) : 0) + "%" } })),
      el("div", { class: "faint map-pbs-sub" }, "shared by every node" + (used != null ? " · " + fmt.bytes(store.used_bytes) + " of " + fmt.bytes(store.total_bytes) + " used" : store && !store.reachable ? " · not reachable" : "")),
      names.map((n) => nodeRow(n, perNode.get(n) || [])),
      perNode.has("") ? nodeRow("", perNode.get("")) : null);
    return el("div", { class: "map-bcol", style: { gridRow: "2 / span " + rowsCount } }, card);
  }

  function draw() {
    I = index();
    const heads = $$(".map-colh", root);
    clear(root); heads.forEach((h) => root.append(h));
    const byNode = new Map();
    for (const t of I.roots) {
      const k = t.node || "";
      if (!byNode.has(k)) byNode.set(k, []);
      byNode.get(k).push(t);
    }
    const order = (a, b) => ((a.guest ? a.guest.vmid : 1e9) - (b.guest ? b.guest.vmid : 1e9)) || row(a.svc.slug).name.localeCompare(row(b.svc.slug).name);
    let n = 0;
    const addRow = (wayIn, middle) => { root.append(el("div", { class: "map-row" }, el("div", { class: "map-way" }, wayIn), el("div", { class: "map-mid" }, middle))); n++; };
    for (const node of V.map.nodes) {
      const all = (byNode.get(node.name) || []).slice().sort(order);
      const nodeHit = q && node.name.toLowerCase().includes(q);
      const list = nodeHit && !problems ? all : all.filter(matches);
      if ((q || problems) && !list.length && !nodeHit) continue;
      const groups = list.filter((t) => I.hostsOf.has(t.svc.slug)).map((t) => hostGroup(t, I.hostsOf.get(t.svc.slug)));
      addRow(groups.length ? groups : el("p", { class: "faint map-none" }, "No proxy hosts lead here."), nodeCard(node, list));
    }
    const unplaced = (byNode.get("") || []).slice().sort(order).filter(matches);
    const looseHosts = I.loose.filter((h) => !problems && (!q || h.hostname.toLowerCase().includes(q)));
    if (unplaced.length || looseHosts.length) {
      const way = unplaced.filter((t) => I.hostsOf.has(t.svc.slug)).map((t) => hostGroup(t, I.hostsOf.get(t.svc.slug)));
      if (looseHosts.length) {
        way.push(el("div", { class: "map-hg" }, el("div", { class: "map-hg-h" }, el("span", { class: "dot off" }), el("span", { class: "nm" }, "Not linked to a service"), el("span", { class: "n" }, String(looseHosts.length))),
          looseHosts.map((h) => el("button", { type: "button", class: "map-host" + (h.served ? "" : " off"), "data-id": h.id, onclick: () => select(h.id) }, el("span", { class: "mono" }, h.hostname)))));
      }
      addRow(way, el("div", { class: "map-node is-loose", "data-id": "node:" },
        el("div", { class: "map-node-h" }, el("strong", {}, "No node")),
        el("div", { class: "map-node-sub" }, "Services without a node, and hosts that lead to no service"),
        unplaced.length ? unplaced.map(svcRow) : el("p", { class: "faint map-none" }, "No services without a node.")));
    }
    if (!n) root.append(el("div", { class: "map-mid dt-empty", style: { gridColumn: "1 / 3" } }, el("strong", {}, q || problems ? "Nothing matches" : "No nodes yet"),
      el("div", {}, q || problems ? "Clear the search or Problems only." : "Import your nodes and guests from Proxmox on Nodes & guests.")));
    root.append(backupsCard(Math.max(1, n)));
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", "map-lines");
    svg.setAttribute("aria-hidden", "true");
    root.append(svg);
    mark();
    sidebar();
    requestAnimationFrame(drawLines);
  }

  // --- selection and lines ---------------------------------------------------------------------
  function related(id) {
    const out = new Set(id ? [id] : []);
    if (!id) return out;
    if (id.startsWith("svc:")) {
      const t = I.svcs.get(id.slice(4));
      if (t) { const walk = (s) => { out.add("svc:" + s.slug); s.children.forEach(walk); }; walk(t.svc); }
    } else if (id.startsWith("node:")) {
      for (const t of I.roots) if (t.node === id.slice(5)) out.add("svc:" + t.svc.slug);
    } else if (id.startsWith("host:")) {
      const h = V.map.entry.hosts.find((x) => x.id === id);
      if (h && h.service) out.add("svc:" + h.service);
    }
    return out;
  }
  function edgesFor(id) {
    if (!id) return [];
    const rel = related(id);
    return V.map.edges.filter((e) => id === "pbs" ? e.from === "pbs" || e.to === "pbs" : rel.has(e.from) || rel.has(e.to));
  }
  function resolve(eid, other) {
    const find = (x) => root.querySelector('[data-id="' + CSS.escape(x) + '"]');
    if (eid === "pbs" && other) {
      const t = I.svcs.get(other.replace(/^svc:/, ""));
      const r = t && find("pbsnode:" + (t.node || ""));
      if (r) return r;
    }
    let e = find(eid);
    if (!e && eid.startsWith("svc:")) {
      const t = I.svcs.get(eid.slice(4));
      if (t) e = find("svc:" + t.root);
    }
    return e && e.offsetParent ? e : null;
  }
  function mark() {
    const rel = related(sel);
    const touched = new Set();
    for (const e of edgesFor(sel)) { touched.add(e.from); touched.add(e.to); }
    $$("[data-id]", root).forEach((x) => {
      const id = x.dataset.id;
      x.classList.toggle("sel", !!sel && (id === sel || (id === "pbs" && sel === "pbs")));
      x.classList.toggle("rel", !!sel && id !== sel && (rel.has(id) || touched.has(id)));
    });
  }
  function drawLines() {
    const svg = $("svg.map-lines", root);
    if (!svg) return;
    clear(svg);
    if (getComputedStyle(svg).display === "none") return;
    const base = root.getBoundingClientRect();
    svg.setAttribute("width", base.width); svg.setAttribute("height", root.scrollHeight);
    const hot = new Set(edgesFor(sel));
    const list = lines === "all" ? V.map.edges : [...hot];
    for (const e of list) {
      const a = resolve(e.from, e.to), b = resolve(e.to, e.from);
      if (!a || !b || a === b) continue;
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      const ya = ra.top + ra.height / 2 - base.top, yb = rb.top + rb.height / 2 - base.top;
      let d;
      if (Math.abs((ra.left + ra.right) / 2 - (rb.left + rb.right) / 2) < 30) {
        const x = Math.max(ra.right, rb.right) - base.left, k = 26 + Math.min(50, Math.abs(ya - yb) / 6);
        d = `M${ra.right - base.left},${ya} C${x + k},${ya} ${x + k},${yb} ${rb.right - base.left},${yb}`;
      } else {
        const l2r = ra.left < rb.left;
        const x1 = (l2r ? ra.right : ra.left) - base.left, x2 = (l2r ? rb.left : rb.right) - base.left;
        const k = Math.max(24, Math.abs(x2 - x1) / 2) * (l2r ? 1 : -1);
        d = `M${x1},${ya} C${x1 + k},${ya} ${x2 - k},${yb} ${x2},${yb}`;
      }
      const path = document.createElementNS(SVG, "path");
      path.setAttribute("d", d);
      path.setAttribute("class", e.kind + (e.off || e.stale ? " faded" : "") + (hot.has(e) ? " hot" : ""));
      const t = document.createElementNS(SVG, "title");
      t.textContent = (e.label || e.kind) + ": " + e.from.replace(/^\w+:/, "") + " → " + e.to.replace(/^\w+:/, "");
      path.append(t);
      svg.append(path);
    }
  }
  function select(id) {
    sel = id;
    state();
    mark();
    sidebar();
    drawLines();
  }
  function state() {
    const p = new URLSearchParams(location.search);
    const set = (k, v) => (v ? p.set(k, v) : p.delete(k));
    set("sel", sel); set("q", q); set("problems", problems ? "1" : "");
    history.replaceState(null, "", location.pathname + (p.toString() ? "?" + p : ""));
  }

  // --- the sidebar -------------------------------------------------------------------------------
  const section = (title, ...kids) => el("section", { class: "map-sec" }, el("h3", {}, title), kids);
  const kv = (pairs) => el("dl", { class: "map-kv" }, pairs.filter(Boolean).map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v == null || v === "" ? "—" : v))));
  const pick = (id, text, cls) => el("button", { type: "button", class: "linkish" + (cls ? " " + cls : ""), onclick: () => select(id) }, text);
  const btn = (label, onClick, primary) => el("button", { type: "button", class: primary ? null : "secondary", onclick: onClick }, label);
  const link = (label, href) => el("a", { class: "btn secondary", href }, label);
  // The datastore answered with a size (a dry run answers with zeros).
  const sized = () => !!(store && store.reachable && store.total_bytes);
  function head(title, pillEl, sub, more, o) {
    o = o || {};
    return el("div", { class: "map-side-h" },
      el("div", { style: { minWidth: 0, flex: 1 } }, el("div", { class: "t" }, el("strong", { class: o.mono ? "mono" : "" }, title), " ", pillEl),
        sub ? el("div", { class: (o.plainSub ? "" : "mono ") + "faint sub" }, sub) : null),
      more && more.length ? menuButton(more, "More actions") : null,
      el("button", { type: "button", class: "icon", "aria-label": "Close", onclick: () => { sel = ""; state(); mark(); sidebar(); drawLines(); } }, "×"));
  }
  function recentRow(ev) {
    const d = O.describeEvent(ev);
    return el("div", { class: "map-ev" }, el("time", { datetime: ev.at, class: "faint mono", title: fmt.time(ev.at) }, fmt.short(ev.at)),
      el("span", {}, el("span", { class: "pill " + d.cls }, d.tag), " ", d.text));
  }
  const why = (cls, ...text) => el("div", { class: "map-why " + cls }, text);

  function sidebar() {
    side.hidden = false;
    side.classList.toggle("open", !!sel);
    document.body.classList.toggle("map-has-sel", !!sel);
    if (!sel) return fill(sideBody, el("div", { class: "map-side-empty" }, el("strong", {}, "Nothing selected"),
      el("p", { class: "faint" }, "Click a node, service or host to see its state, why, what to do about it and the path traffic and backups take."),
      kv([["Nodes", String(V.map.counts.nodes)], ["Services", String(V.map.counts.services) + (V.map.counts.sub_services ? " + " + V.map.counts.sub_services + " sub" : "")], ["Proxy hosts", String(V.map.counts.hosts)]])));
    if (sel.startsWith("svc:") && I.svcs.has(sel.slice(4))) return serviceSide(I.svcs.get(sel.slice(4)));
    if (sel.startsWith("node:") && V.map.nodes.some((n) => n.name === sel.slice(5))) return nodeSide(sel.slice(5));
    if (sel.startsWith("host:")) { const h = V.map.entry.hosts.find((x) => x.id === sel); if (h) return hostSide(h); }
    if (sel === "pbs") return pbsSide();
    sel = ""; state(); sidebar();
  }

  function pathList(t) {
    const r = row(t.svc.slug);
    const hosts = I.hostsOf.get(t.root) || [];
    const steps = [];
    const own = hosts.filter((h) => h.service === t.svc.slug);
    (own.length ? own : hosts).slice(0, 3).forEach((h) => steps.push([el("span", { class: "c-proxy" }, "traffic"), " ", h.hostname, " via Traefik"]));
    if (t.root !== t.svc.slug) steps.push(["runs inside ", pick("svc:" + t.root, row(t.root).name)]);
    else if (t.node) steps.push(["runs as ", t.guest ? guestLabel(t.guest) : "a service", " on ", el("strong", {}, t.node), " (" + power(t.node) + ")"]);
    const node = nodeOf(t.node);
    if (node && !node.always_on) steps.push([el("span", { class: "c-requires" }, "needs awake"), " " + t.node + (canWake(node) ? ", woken by the power manager" : ", which has no power target")]);
    for (const e of V.map.edges) {
      if (e.kind === "depends" && e.from === "svc:" + t.svc.slug) steps.push([el("span", { class: "c-depends" }, "depends on"), " ", pick(e.to, e.to.slice(4))]);
      if (e.kind === "requires" && e.to === "svc:" + t.svc.slug) steps.push([el("span", { class: "c-requires" }, "kept awake"), " for " + e.label.replace(/: needs awake$/, "")]);
    }
    const pol = t.root === t.svc.slug ? r.backup.main : "";
    if (pol && pol !== "no policy" && pol !== "excluded") steps.push([el("span", { class: "c-backup" }, "backs up to"), " PBS · " + (V.map.backup.datastore || "datastore") + " (" + pol + ")"]);
    return el("ol", { class: "map-path" }, steps.map((s) => el("li", {}, s)));
  }

  function serviceSide(t) {
    const r = row(t.svc.slug);
    const node = nodeOf(t.node);
    const p = t.node ? power(t.node) : "";
    const refresh = refreshAll;
    const sub = [t.svc.slug, t.root !== t.svc.slug ? "sub-service of " + t.root : t.guest ? guestLabel(t.guest) + (t.node ? " on " + t.node : "") : t.node ? "on " + t.node : "no node"].join(" · ");
    let whyEl = null, primary = null;
    const bulk = (action) => () => O.bulk("services", action, [t.svc.slug]).then(refresh, () => {});
    if (r.state.label === "asleep" && t.node) {
      whyEl = why("warn", "Not broken: its node ", el("strong", {}, t.node), " is off. " + (canWake(node) ? "Wake it and " + r.name + " comes back." : "It has no power target, so it cannot be woken from here."));
      if (canWake(node)) primary = btn("Wake " + t.node, () => O.nodePower(node, "wake", refresh), true);
    } else if (r.state.label === "stopped") {
      whyEl = why("warn", "Its guest is stopped. Waking the service starts it.");
      primary = btn("Wake " + r.name, bulk("wake"), true);
    } else if (r.state.cls === "fail") {
      whyEl = why("fail", r.state.why || "Its last check failed.");
      primary = btn("Check now", bulk("resync"), true);
    } else if (r.state.cls === "warn" || (r.state.cls !== "ok" && r.state.why)) {
      whyEl = why(r.state.cls === "warn" ? "warn" : "off", r.state.why);
    }
    const acts = [
      r.state.cls === "fail" ? null : btn("Check now", bulk("resync")),
      r.backup_policy_id != null && !r.parent ? btn("Back up now", bulk("backup")) : null,
      link("Open service", "/services/" + enc(t.svc.slug)),
      btn("Edit…", () => O.serviceDrawer({ service: r, view: V.view, onSaved: refresh })),
    ].filter(Boolean);
    const hosts = (I.hostsOf.get(t.root) || []).filter((h) => t.root !== t.svc.slug ? h.service === t.svc.slug : true);
    const recent = el("div", { class: "map-recent" }, el("p", { class: "faint" }, "Loading…"));
    api.get("/api/activity?limit=5&service=" + enc(t.svc.slug), { suppressToast: true }).then((list) => {
      fill(recent, list.length ? list.map(recentRow) : el("p", { class: "faint" }, "Nothing yet."));
    }, () => fill(recent, el("p", { class: "faint" }, "Could not load its activity.")));
    fill(sideBody,
      head(r.name, pill(r.state.cls, r.state.label, r.state.why), sub, O.serviceActions(r, V.view, refresh).filter((x) => x && x !== "-" && !/^(Open|Check now|Run a backup now|Edit settings…)$/.test(x.label))),
      whyEl,
      el("div", { class: "map-acts" }, primary, el("div", { class: "grid" }, acts)),
      section("Details", kv([
        ["Node", t.node ? [pick("node:" + t.node, t.node), " ", pill(powerCls(p), p)] : "—"],
        ["Guest", t.guest ? guestLabel(t.guest) + " · " + t.guest.status : r.parent ? "its parent's" : "none"],
        ["Address", t.svc.ip ? el("span", { class: "mono" }, t.svc.ip) : "—"],
        ["Wake on hit", r.wake_on_hit],
      ])),
      section("Proxy hosts", hosts.length ? el("div", { class: "map-chips" }, hosts.map((h) => el("div", {}, pick(h.id, h.hostname, "mono"), " ", pill(h.served ? "ok" : "off", h.served ? "on" : "off"))))
        : el("p", { class: "faint" }, "No proxy host leads here. ", el("a", { href: "/proxy-hosts?new=1" + (r.id != null ? "&service_id=" + r.id : "") }, "Add one"))),
      section("The path", pathList(t)),
      section("Backups", kv([
        ["Policy", r.backup.main + (I.policies.get(r.backup.main) && I.policies.get(r.backup.main).next_run ? " · next " + fmt.short(I.policies.get(r.backup.main).next_run) : "")],
        ["Last", r.backup.at ? fmt.short(r.backup.at) + " · " + r.backup.sub : r.backup.sub || "—"],
      ]), el("a", { href: "/activity?kind=backup&service=" + enc(t.svc.slug) }, "All backups of " + r.name + " →")),
      t.svc.children.length ? section("Sub-services", el("div", { class: "map-chips" }, t.svc.children.map((c) => el("div", {}, pick("svc:" + c.slug, row(c.slug).name), " ", pill(row(c.slug).state.cls, row(c.slug).state.label))))) : null,
      section("Recent", recent));
  }

  function nodeSide(name) {
    const node = nodeOf(name) || { name, always_on: false, power_mgr_target: "", service_slugs: [] };
    const m = V.map.nodes.find((n) => n.name === name);
    const p = power(name);
    const l = live && live.nodes.find((x) => x.name === name);
    const h = l && l.health;
    const mine = I.roots.filter((t) => t.node === name);
    const refresh = refreshAll;
    let whyEl = null;
    if (p === "off") whyEl = why("warn", "Off. Its services sleep until it is woken" + (canWake(node) ? (node.wake_on_hit_enabled ? "; a visit to one of them wakes it." : ".") : ", and it has no power target to wake it with."));
    else if (p !== "on") whyEl = why("off", "Its power state is not known yet" + (h && h.error ? ": " + h.error : "."));
    const primary = p === "off" && canWake(node) ? btn("Wake " + name, () => O.nodePower(node, "wake", refresh), true) : null;
    const acts = [
      btn("Check", () => nodeStatus.refresh()),
      link("Open on Nodes & guests", "/nodes?q=" + enc(name)),
      node.id != null ? btn("Edit…", () => O.nodeDrawer({ node, onSaved: refresh })) : null,
    ].filter(Boolean);
    const more = [
      canWake(node) ? { label: "Wake", onClick: () => O.nodePower(node, "wake", refresh) } : null,
      node.power_mgr_target ? { label: "Restart…", onClick: () => O.nodePower(node, "restart", refresh) } : null,
      node.power_mgr_target ? { label: "Power off…", danger: true, onClick: () => O.nodePower(node, "power_off", refresh) } : null,
    ].filter(Boolean);
    const targets = V.map.backup.targets.filter((t) => (I.svcs.get(t.slug) || {}).node === name);
    fill(sideBody,
      head(name, pill(powerCls(p), p), "node · " + (m && m.address ? m.address : "no address"), more),
      whyEl,
      el("div", { class: "map-acts" }, primary, el("div", { class: "grid" }, acts)),
      section("Details", kv([
        ["Power", node.always_on ? "always on" : canWake(node) ? "wakes on demand" : "no power target"],
        ["Power target", node.power_mgr_target ? el("span", { class: "mono" }, node.power_mgr_target) : "—"],
        ["Wake on hit", node.wake_on_hit_enabled === false ? "off" : "on"],
        ["Proxmox", h && h.reachable ? [h.version || "answers", h.latency_ms != null ? " · " + Math.round(h.latency_ms) + " ms" : ""] : live ? "not answering" : "checking…"],
      ])),
      section("Services (" + mine.length + ")", mine.length ? el("div", { class: "map-chips" }, mine.map((t) => el("div", {}, pick("svc:" + t.svc.slug, row(t.svc.slug).name), " ", el("span", { class: "faint mono" }, guestLabel(t.guest)), " ", pill(row(t.svc.slug).state.cls, row(t.svc.slug).state.label))))
        : el("p", { class: "faint" }, "None yet. ", el("a", { href: "/nodes?q=" + enc(name) }, "Import its guests"))),
      section("Backups", targets.length ? kv([["Guests backed up", String(targets.length)], ["Policies", [...new Set(targets.map((t) => t.policy).filter(Boolean))].join(", ") || "none"]])
        : el("p", { class: "faint" }, "Nothing on this node is backed up yet.")));
  }

  function hostSide(h) {
    const t = h.service && I.svcs.get(h.service);
    const steps = [[el("span", { class: "c-proxy" }, "traffic"), " ", h.hostname, " via Traefik"]];
    if (t) {
      steps.push(["to ", pick("svc:" + t.svc.slug, row(t.svc.slug).name), t.svc.ip ? el("span", { class: "mono faint" }, " " + t.svc.ip) : null]);
      if (t.node) steps.push(["on ", pick("node:" + t.node, t.node), " (" + power(t.node) + ")"]);
    }
    fill(sideBody,
      head(h.hostname, pill(h.served ? "ok" : "off", h.served ? "on" : "off"), "proxy host · " + h.provider, null, { mono: true }),
      h.served ? null : why("off", h.enabled ? "Not routed: the service it leads to is switched off." : "Switched off: Traefik does not route it."),
      el("div", { class: "map-acts" }, el("div", { class: "grid" },
        h.pk != null ? link("Edit…", "/proxy-hosts?edit=" + h.pk) : null,
        t ? btn("Select its service", () => select("svc:" + t.svc.slug)) : null,
        link("All proxy hosts", "/proxy-hosts?q=" + enc(h.hostname)))),
      section("Details", kv([["Service", t ? pick("svc:" + t.svc.slug, row(t.svc.slug).name) : "none: not linked"], ["Router", h.provider]])),
      section("The path", el("ol", { class: "map-path" }, steps.map((s) => el("li", {}, s)))));
  }

  function pbsSide() {
    const b = V.map.backup;
    const used = sized() ? store : null;
    const noPolicy = b.targets.filter((t) => !t.policy);
    fill(sideBody,
      head("PBS · " + (b.datastore || "datastore"), used ? pill(used.used_fraction > 0.85 ? "warn" : "ok", Math.round(used.used_fraction * 100) + "% used") : pill("off", !store ? "checking" : store.reachable ? "size unknown" : "not reachable"), "backup server shared by every node", null, { plainSub: true }),
      store && !store.reachable ? why("warn", "Could not reach it: " + (store.error || "no answer") + ".") : null,
      el("div", { class: "map-acts" }, el("div", { class: "grid" }, link("Open Backups", "/backup-policies"), link("Snapshots", "/backup-policies?tab=snapshots"))),
      section("Details", kv([
        ["Datastore", b.datastore || "—"], ["Proxmox storage", b.storage || "—"],
        ["Space", used ? fmt.bytes(used.used_bytes) + " of " + fmt.bytes(used.total_bytes) : "—"],
        ["Guests in scope", String(b.targets.length)],
      ])),
      section("Policies", V.policies.length ? kv(V.policies.map((p) => [p.name, p.next_run ? "next " + fmt.short(p.next_run) : "not scheduled"])) : el("p", { class: "faint" }, "No policies yet.")),
      noPolicy.length ? section("No policy (" + noPolicy.length + ")", el("div", { class: "map-chips" }, noPolicy.map((t) => el("div", {}, pick("svc:" + t.slug, row(t.slug).name))))) : null);
  }

  // --- live ------------------------------------------------------------------------------------------
  const view = region(el("div"), {
    initial: V,
    load: () => api.get("/api/views/map", { suppressToast: true }),
    render: (d) => { V = d; draw(); },
    topics: ["health", "run", "registry"], wait: 1500, age: $("#map-age"),
  });
  const nodeStatus = region(el("div"), {
    load: () => api.get("/api/infra/nodes/status", { suppressToast: true }),
    render: (d) => { live = d; draw(); },
    topics: ["run"], wait: 3000, every: 60000, poll: true,
  });
  function refreshAll() { view.refresh(); nodeStatus.refresh(); }
  api.get("/api/infra/pbs/datastore", { suppressToast: true }).then((d) => { store = d; draw(); }, () => {});

  // --- controls -------------------------------------------------------------------------------------
  const qIn = $("#map-q"), probBtn = $("#map-problems");
  qIn.value = q;
  probBtn.setAttribute("aria-pressed", String(problems));
  let qt = 0;
  qIn.addEventListener("input", () => { clearTimeout(qt); qt = setTimeout(() => { q = qIn.value.trim().toLowerCase(); state(); draw(); }, 150); });
  probBtn.addEventListener("click", () => { problems = !problems; probBtn.setAttribute("aria-pressed", String(problems)); state(); draw(); });
  $$("[data-lines]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.lines === lines));
    b.addEventListener("click", () => {
      lines = b.dataset.lines;
      try { localStorage.setItem("orch:map:lines", lines); } catch (_e) { /* ignore */ }
      $$("[data-lines]").forEach((o) => o.setAttribute("aria-pressed", String(o === b)));
      drawLines();
    });
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && sel && !document.querySelector(".drawer, .scrim")) { sel = ""; state(); mark(); sidebar(); drawLines(); } });
  let rt = 0;
  window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(drawLines, 120); });
  draw();
})();
