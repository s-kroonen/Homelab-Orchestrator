/* The shared building blocks of every page (docs/design/README.md). Loaded once by base.html,
 * after app.js (toast, apiFetch, OrchTime) and live.js (OrchLive).
 *
 *   UI.$ / UI.$$ / UI.el                 DOM helpers, so pages stop defining their own
 *   UI.api.get/post/put/del(url, body)   apiFetch with the error toast
 *   UI.fmt.time/ago/bytes                one way to show a time or a size
 *   UI.drawer({...})                     every create and edit: fields, inline errors, Test, Save
 *   UI.confirm({...})                    every destructive action: what else is affected, type the name
 *   UI.menu(button, items)               the ⋯ row and header menus
 *   UI.tabs(root)                        tabs that live in the URL (?tab=)
 *   UI.region(el, {...})                 a section that loads itself, refreshes on live events and
 *                                        says how old it is; never a page reload
 *   UI.table(el, {...})                  filter box, chips, sortable columns, row menu, bulk bar,
 *                                        empty state, cards on a phone
 */
(function () {
  "use strict";

  // --- DOM ---------------------------------------------------------------------------------
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // el("a", {href: "/x", class: "btn", onclick: fn, dataset: {id: 1}}, "text", node, [more])
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "dataset") Object.assign(node.dataset, v);
      else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, "");
      else node.setAttribute(k, String(v));
    }
    append(node, children);
    return node;
  }
  function append(node, children) {
    for (const c of children.flat(Infinity)) {
      if (c == null || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }
  // Replace a node's children; arrays (nested too), null and false are fine.
  function fill(node, ...children) { return append(clear(node), children); }
  const pill = (cls, text) => el("span", { class: "pill " + (cls || "off") }, text);

  // --- API and formatting ----------------------------------------------------------------------
  const api = {
    get: (url, opts) => window.apiFetch("GET", url, undefined, opts),
    post: (url, body, opts) => window.apiFetch("POST", url, body === undefined ? {} : body, opts),
    put: (url, body, opts) => window.apiFetch("PUT", url, body, opts),
    patch: (url, body, opts) => window.apiFetch("PATCH", url, body, opts),
    del: (url, opts) => window.apiFetch("DELETE", url, undefined, opts),
  };
  const fmt = {
    time: (iso) => (window.OrchTime ? window.OrchTime.fmt(iso) : String(iso || "")),
    ago: (iso) => (window.OrchTime ? window.OrchTime.ago(iso) : ""),
    // "14:58" today, "yesterday 03:12", "Mon 09:12" this week, else "27 Oct 14:58".
    short(iso) {
      if (!iso || !window.OrchTime) return String(iso || "");
      const d = new Date(iso); if (isNaN(d)) return String(iso);
      const T = window.OrchTime;
      const hm = T.fmt(iso, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
      const day = (x) => T.fmt(x.toISOString(), { year: "numeric", month: "2-digit", day: "2-digit" });
      const now = new Date();
      if (day(d) === day(now)) return hm;
      if (day(d) === day(new Date(now.getTime() - 86400000))) return "yesterday " + hm;
      if (now - d < 6 * 86400000) return T.fmt(iso, { weekday: "short" }) + " " + hm;
      return T.fmt(iso, { day: "2-digit", month: "short" }) + " " + hm;
    },
    secondsAgo(ms) {
      const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
      if (s < 5) return "just now";
      if (s < 90) return s + " s ago";
      if (s < 5400) return Math.round(s / 60) + " min ago";
      return Math.round(s / 3600) + " h ago";
    },
    bytes(n) {
      if (n == null || isNaN(n)) return "";
      const u = ["B", "KB", "MB", "GB", "TB", "PB"];
      let i = 0; let v = Number(n);
      while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
      return (v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)) + " " + u[i];
    },
  };

  // --- layers: one open overlay at a time per kind, Escape closes the top one ----------------------
  const layers = [];
  function pushLayer(close) { layers.push(close); return () => { const i = layers.indexOf(close); if (i >= 0) layers.splice(i, 1); }; }
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && layers.length) { ev.preventDefault(); layers[layers.length - 1](); }
  });
  function trapFocus(box) {
    box.addEventListener("keydown", (ev) => {
      if (ev.key !== "Tab") return;
      const f = $$("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])", box)
        .filter((n) => !n.disabled && n.offsetParent !== null);
      if (!f.length) return;
      const first = f[0]; const last = f[f.length - 1];
      if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
      else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
    });
  }

  // --- the ⋯ menu ------------------------------------------------------------------------------
  let openMenu = null;
  function closeMenu() { if (openMenu) { openMenu(); openMenu = null; } }
  // items: [{label, onClick, href, danger, disabled, title}, "-" for a separator, {heading}]
  function menu(anchor, items) {
    closeMenu();
    const box = el("div", { class: "menu", role: "menu" });
    for (const it of items.filter(Boolean)) {
      if (it === "-") { box.append(el("hr")); continue; }
      if (it.heading) { box.append(el("div", { class: "label" }, it.heading)); continue; }
      const cls = it.danger ? "danger" : null;
      const node = it.href
        ? el("a", { href: it.href, role: "menuitem", class: cls }, it.label)
        : el("button", { type: "button", role: "menuitem", class: cls, disabled: !!it.disabled, title: it.title }, it.label);
      node.addEventListener("click", () => { closeMenu(); if (it.onClick) it.onClick(); });
      box.append(node);
    }
    document.body.append(box);
    const r = anchor.getBoundingClientRect();
    const w = box.offsetWidth; const h = box.offsetHeight;
    box.style.left = Math.max(8, Math.min(r.right - w, innerWidth - w - 8)) + "px";
    box.style.top = (r.bottom + 4 + h > innerHeight - 8 ? Math.max(8, r.top - h - 4) : r.bottom + 4) + "px";
    anchor.setAttribute("aria-expanded", "true");
    const onDoc = (ev) => { if (!box.contains(ev.target) && ev.target !== anchor) closeMenu(); };
    const pop = pushLayer(closeMenu);
    setTimeout(() => document.addEventListener("click", onDoc), 0);
    window.addEventListener("scroll", closeMenu, { once: true, capture: true });
    openMenu = () => {
      box.remove(); pop(); document.removeEventListener("click", onDoc);
      anchor.setAttribute("aria-expanded", "false"); anchor.focus({ preventScroll: true });
    };
    const first = box.querySelector("button:not(:disabled), a");
    if (first) first.focus();
    box.addEventListener("keydown", (ev) => {
      if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
      ev.preventDefault();
      const f = $$("button:not(:disabled), a", box);
      const i = f.indexOf(document.activeElement);
      f[(i + (ev.key === "ArrowDown" ? 1 : -1) + f.length) % f.length].focus();
    });
  }
  function menuButton(items, label) {
    return el("button", {
      type: "button", class: "icon", "aria-label": label || "More actions", "aria-haspopup": "menu", "aria-expanded": "false",
      onclick: (ev) => { ev.stopPropagation(); menu(ev.currentTarget, typeof items === "function" ? items() : items); },
    }, "⋯");
  }

  // --- confirm -------------------------------------------------------------------------------
  // confirm({title, body, affected: [..], typeName: "node-a", action: "Power off", danger: true})
  // resolves true when confirmed. typeName: the button stays disabled until the name is typed.
  function confirm(opts) {
    return new Promise((resolve) => {
      const scrim = el("div", { class: "scrim over" });
      const action = el("button", { type: "button", class: opts.danger === false ? "" : "danger-solid" }, opts.action || "Confirm");
      const cancel = el("button", { type: "button", class: "secondary" }, opts.cancel || "Cancel");
      let input = null;
      const box = el("div", { class: "dialog", role: "alertdialog", "aria-modal": "true", "aria-labelledby": "dlg-title" },
        el("h2", { id: "dlg-title" }, opts.title || "Are you sure?"),
        opts.body ? (opts.body instanceof Node ? opts.body : el("p", {}, opts.body)) : null,
        opts.affected && opts.affected.length ? [el("div", { class: "lane" }, opts.affectedLabel || "This also affects"),
          el("ul", { class: "affected" }, opts.affected.map((a) => el("li", {}, a)))] : null,
        opts.typeName ? el("div", { class: "field type-name" },
          el("label", { for: "dlg-type" }, "Type ", el("code", {}, opts.typeName), " to confirm"),
          input = el("input", { id: "dlg-type", autocomplete: "off", spellcheck: "false" })) : null,
        el("div", { class: "buttons" }, cancel, action));
      if (input) {
        action.disabled = true;
        input.addEventListener("input", () => { action.disabled = input.value.trim() !== opts.typeName; });
        input.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !action.disabled) done(true); });
      }
      let pop = null;
      const done = (ok) => { pop(); scrim.remove(); box.remove(); resolve(ok); };
      pop = pushLayer(() => done(false));
      cancel.onclick = () => done(false);
      action.onclick = () => done(true);
      scrim.onclick = () => done(false);
      trapFocus(box);
      document.body.append(scrim, box);
      (input || cancel).focus();
    });
  }

  // --- drawer --------------------------------------------------------------------------------
  // drawer({
  //   title, sub, values: {}, saveLabel: "Save", note: "Writes services.yaml and pushes to TM",
  //   fields: [{key, label, type: text|number|select|checkbox|textarea|password|section|custom,
  //             options: [{value, label}] | [value], help, placeholder, required, mono, readonly,
  //             show: (values) => bool, render: (values, set) => Node (custom)}],
  //   onSave: async (values) => {...} (throw an apiFetch error: fieldErrors land under the field),
  //   onTest: async (values) => "message" (optional), testLabel,
  //   onDelete: async () => {...} (optional, shown left in the footer),
  //   wide: true (for a log or a long text: nearly the whole width),
  // })
  let openDrawer = null;
  function drawer(opts) {
    if (openDrawer) openDrawer.close(true);
    const values = Object.assign({}, opts.values || {});
    const initial = JSON.stringify(values);
    const scrim = el("div", { class: "scrim" });
    const body = el("div", { class: "drawer-body" });
    const formError = el("div", { class: "banner fail form-error", role: "alert", hidden: true });
    const testResult = el("div", { class: "test-result", hidden: true, "aria-live": "polite" });
    const save = el("button", { type: "submit" }, opts.saveLabel || "Save");
    const cancel = el("button", { type: "button", class: "secondary" }, "Cancel");
    const test = opts.onTest ? el("button", { type: "button", class: "secondary" }, opts.testLabel || "Test") : null;
    const del = opts.onDelete ? el("button", { type: "button", class: "danger" }, opts.deleteLabel || "Delete") : null;
    const closeBtn = el("button", { type: "button", class: "icon", "aria-label": "Close" }, "✕");
    const form = el("form", { class: "drawer" + (opts.wide ? " wide" : ""), role: "dialog", "aria-modal": "true", "aria-labelledby": "drawer-title", novalidate: true },
      el("div", { class: "drawer-head" },
        el("div", {}, el("h2", { id: "drawer-title" }, opts.title || "Edit"), opts.sub ? el("div", { class: "sub" }, opts.sub) : null),
        closeBtn),
      body,
      el("div", { class: "drawer-foot" },
        del, el("div", { class: "note" }, opts.note || ""), test, cancel, save, testResult));
    const controls = {};
    const rows = [];

    function set(key, v) { values[key] = v; refreshVisibility(); }
    function refreshVisibility() {
      for (const r of rows) if (r.field.show) r.node.hidden = !r.field.show(values);
    }
    function build() {
      body.append(formError);
      if (opts.intro) body.append(opts.intro instanceof Node ? opts.intro : el("p", { class: "page-sub" }, opts.intro));
      for (const f of opts.fields || []) {
        let node;
        if (f.type === "section") node = el("h3", {}, f.label);
        else if (f.type === "custom") node = el("div", { class: "field" }, f.label ? el("div", { class: "label" }, f.label) : null, f.render(values, set));
        else node = fieldRow(f);
        rows.push({ field: f, node });
        body.append(node);
      }
      refreshVisibility();
    }
    function fieldRow(f) {
      const id = "f-" + f.key.replace(/[^\w-]/g, "_");
      const err = el("div", { class: "field-error", id: id + "-err", hidden: true });
      const help = f.help ? el("div", { class: "help", id: id + "-help" }, f.help) : null;
      const describedBy = [help ? id + "-help" : null, id + "-err"].filter(Boolean).join(" ");
      let input;
      const v = values[f.key];
      if (f.type === "checkbox") {
        input = el("input", { type: "checkbox", id, "aria-describedby": describedBy, disabled: !!f.readonly });
        input.checked = !!v;
        input.addEventListener("change", () => set(f.key, input.checked));
        controls[f.key] = { input, err };
        return el("div", { class: "field check" }, input, el("label", { for: id }, f.label), help, err);
      }
      if (f.type === "checklist") {
        // value: an array of the checked option values; options: [{value, label, note, warn}]
        const chosen = new Set(Array.isArray(v) ? v : []);
        const box = el("div", { class: "checklist", id, role: "group", "aria-describedby": describedBy });
        for (const o of f.options || []) {
          const cb = el("input", { type: "checkbox", value: o.value, disabled: !!f.readonly });
          cb.checked = chosen.has(o.value);
          cb.addEventListener("change", () => set(f.key, Array.from(box.querySelectorAll("input:checked")).map((x) => x.value)));
          box.append(el("label", { class: "inline" }, cb, " ", o.label, o.note ? el("span", { class: "faint" }, " " + o.note) : null,
            o.warn ? el("span", { class: "pill fail", title: o.warn, style: { marginLeft: "4px" } }, "cannot be woken") : null));
        }
        if (!(f.options || []).length) box.append(el("span", { class: "faint" }, f.empty || "Nothing to choose."));
        controls[f.key] = { input: box, err };
        return el("div", { class: "field" }, el("div", { class: "label" }, f.label), help, box, err);
      }
      if (f.type === "select") {
        input = el("select", { id, "aria-describedby": describedBy, disabled: !!f.readonly });
        for (const o of f.options || []) {
          const opt = typeof o === "object" ? o : { value: o, label: o };
          input.append(el("option", { value: opt.value }, opt.label));
        }
        input.value = v == null ? "" : String(v);
        input.addEventListener("change", () => set(f.key, input.value));
      } else if (f.type === "textarea") {
        input = el("textarea", { id, rows: f.rows || 4, placeholder: f.placeholder, "aria-describedby": describedBy, readonly: !!f.readonly });
        input.value = v == null ? "" : String(v);
        input.addEventListener("input", () => set(f.key, input.value));
      } else {
        input = el("input", {
          id, type: f.type || "text", placeholder: f.placeholder, "aria-describedby": describedBy,
          readonly: !!f.readonly, required: !!f.required, autocomplete: "off", spellcheck: "false",
          class: f.mono ? "mono" : null, list: f.list, min: f.min, max: f.max, step: f.step,
        });
        input.value = v == null ? "" : String(v);
        input.addEventListener("input", () => set(f.key, f.type === "number" ? (input.value === "" ? null : Number(input.value)) : input.value));
      }
      controls[f.key] = { input, err };
      return el("div", { class: "field" }, el("label", { for: id }, f.label, f.required ? el("span", { class: "faint" }, " *") : null), input, help, err);
    }
    function clearErrors() {
      formError.hidden = true;
      for (const c of Object.values(controls)) { c.err.hidden = true; c.input.removeAttribute("aria-invalid"); }
    }
    function showError(e) {
      const fe = (e && e.fieldErrors) || {};
      let placed = false;
      for (const [k, m] of Object.entries(fe)) {
        const c = controls[k];
        if (!c) continue;
        c.err.textContent = m; c.err.hidden = false; c.input.setAttribute("aria-invalid", "true");
        if (!placed) c.input.focus();
        placed = true;
      }
      if (!placed || (e && e.message && !Object.keys(fe).length)) {
        formError.textContent = (e && e.message) || "Could not save";
        formError.hidden = false;
        formError.scrollIntoView({ block: "nearest" });
      }
    }
    function missingRequired() {
      let bad = false;
      for (const r of rows) {
        const f = r.field;
        if (!f.required || r.node.hidden) continue;
        const v = values[f.key];
        if (v == null || String(v).trim() === "") {
          const c = controls[f.key];
          c.err.textContent = "Required"; c.err.hidden = false; c.input.setAttribute("aria-invalid", "true");
          if (!bad) c.input.focus();
          bad = true;
        }
      }
      return bad;
    }

    let pop = null;
    const handle = {
      el: form, values, set,
      close(force) {
        if (!force && JSON.stringify(values) !== initial && !window.confirm("Discard your changes?")) return;
        pop(); scrim.remove(); form.remove(); openDrawer = null;
        if (opts.onClose) opts.onClose();
      },
      error: showError,
    };
    pop = pushLayer(() => handle.close());
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      clearErrors();
      if (missingRequired()) return;
      save.disabled = true;
      const label = save.textContent; save.textContent = "Saving…";
      try {
        await opts.onSave(values, handle);
        handle.close(true);
      } catch (e) {
        showError(e);
      } finally {
        save.disabled = false; save.textContent = label;
      }
    });
    cancel.onclick = () => handle.close();
    closeBtn.onclick = () => handle.close();
    scrim.onclick = () => handle.close();
    if (test) test.onclick = async () => {
      test.disabled = true; testResult.hidden = false; testResult.className = "test-result muted"; testResult.textContent = "Testing…";
      try {
        const m = await opts.onTest(values);
        testResult.className = "test-result msg ok"; testResult.textContent = m || "Works";
      } catch (e) {
        testResult.className = "test-result msg error"; testResult.textContent = (e && e.message) || "Failed";
      } finally { test.disabled = false; }
    };
    if (del) del.onclick = async () => {
      try { if (await opts.onDelete(handle) !== false) handle.close(true); } catch (e) { showError(e); }
    };
    build();
    trapFocus(form);
    document.body.append(scrim, form);
    openDrawer = handle;
    const first = form.querySelector(".drawer-body input:not([readonly]), .drawer-body select, .drawer-body textarea");
    (first || closeBtn).focus();
    return handle;
  }

  // --- tabs ---------------------------------------------------------------------------------
  // <div class="tabs" role="tablist" data-param="tab"><button role="tab" data-tab="a">A</button>...</div>
  // <div role="tabpanel" data-tab="a">...</div>. The open tab is kept in ?tab= (replaceState).
  function tabs(root, opts) {
    const o = opts || {};
    const param = o.param || root.dataset.param || "tab";
    const btns = $$("[role=tab]", root);
    const scope = o.panels || document;
    const panels = () => $$("[role=tabpanel][data-tab]", scope);
    function show(name, push) {
      if (!btns.some((b) => b.dataset.tab === name)) name = btns[0] && btns[0].dataset.tab;
      for (const b of btns) {
        const on = b.dataset.tab === name;
        b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1;
      }
      for (const p of panels()) p.hidden = p.dataset.tab !== name;
      if (push) {
        const u = new URL(location.href);
        if (name === (btns[0] && btns[0].dataset.tab)) u.searchParams.delete(param); else u.searchParams.set(param, name);
        history.replaceState(history.state, "", u);
      }
      root.dispatchEvent(new CustomEvent("tabchange", { detail: name }));
      if (o.onChange) o.onChange(name);
    }
    for (const b of btns) {
      b.type = "button";
      b.addEventListener("click", () => show(b.dataset.tab, true));
      b.addEventListener("keydown", (ev) => {
        if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
        const i = btns.indexOf(b) + (ev.key === "ArrowRight" ? 1 : -1);
        const n = btns[(i + btns.length) % btns.length]; n.focus(); show(n.dataset.tab, true);
      });
    }
    show(new URLSearchParams(location.search).get(param) || o.initial, false);
    return { show: (n) => show(n, true), get current() { const b = btns.find((x) => x.getAttribute("aria-selected") === "true"); return b && b.dataset.tab; } };
  }

  // --- live sections ---------------------------------------------------------------------------
  // region(el, {load: async () => data, render: (data, el) => void, topics: ["registry"], wait: 800,
  //             filter: (eventData) => bool, every: 60000, age: element showing "updated 8 s ago",
  //             initial: data already in the page (first paint without a request)})
  const ageEls = new Set();
  setInterval(() => { for (const a of ageEls) a.tick(); }, 5000);
  function region(node, opts) {
    const o = Object.assign({ wait: 800, every: 60000 }, opts);
    let loadedAt = 0; let busy = false; let again = false; let failed = false; let data = null;
    const ageEl = o.age || null;
    const tick = () => {
      if (!ageEl) return;
      if (!loadedAt) { ageEl.textContent = busy ? "loading…" : ""; return; }
      ageEl.textContent = (failed ? "refresh failed · " : "") + "updated " + fmt.secondsAgo(loadedAt);
      ageEl.classList.toggle("error", failed);
      ageEl.title = fmt.time(new Date(loadedAt).toISOString());
    };
    if (ageEl) ageEls.add({ tick });
    function skeleton() {
      const s = el("div", { class: "skeleton", "aria-label": "Loading" });
      for (let i = 0; i < (o.skeletonRows || 4); i++) s.append(el("i"));
      clear(node).append(s);
    }
    async function refresh() {
      if (busy) { again = true; return; }
      if (document.hidden && loadedAt) { stale = true; return; }
      busy = true;
      if (!loadedAt && !data) { skeleton(); tick(); }
      try {
        data = await o.load();
        failed = false;
        o.render(data, node);
        loadedAt = Date.now();
      } catch (e) {
        failed = true;
        if (!data) clear(node).append(el("div", { class: "region-error" }, "Could not load: " + ((e && e.message) || e), " ",
          el("button", { type: "button", class: "small secondary", onclick: () => refresh() }, "Try again")));
        if (!loadedAt) loadedAt = Date.now();
      } finally {
        busy = false; tick();
        if (again) { again = false; refresh(); }
      }
    }
    let stale = false;
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && (stale || Date.now() - loadedAt > o.every)) { stale = false; refresh(); }
    });
    if (o.topics && window.OrchLive) {
      window.OrchLive.on(o.topics, (d) => { if (!o.filter || o.filter(d)) refresh(); }, { wait: o.wait });
    }
    if (o.every) setInterval(() => { if (!window.OrchLive || !window.OrchLive.connected || o.poll) refresh(); }, o.every);
    if (o.initial !== undefined) {
      data = o.initial; o.render(data, node); loadedAt = Date.now(); tick();
      if (o.refreshInitial) refresh();
    } else refresh();
    return { refresh, get data() { return data; } };
  }

  // --- data table --------------------------------------------------------------------------------
  // table(container, {
  //   columns: [{key, label, sort: (r) => value | true, render: (r) => Node|string, cls, phone: "label" | false}],
  //   rows: [], key: (r) => id, search: (r) => "text to match", placeholder,
  //   chips: [{key, label, test: (r) => bool}], menu: (r) => [items], href: (r) => url (name column link),
  //   bulk: (rows) => [{label, onClick, danger}], empty: {title, text, action: Node},
  //   sort: {key, dir: 1|-1}, noun: ["service", "services"], urlState: true, children: (r) => parentKey,
  //   groupBy: [{key: "node", label: "Node", of: (r) => "node-a"}] (adds a "Group by" select),
  //   plain: true (inside a card already: no card, filter box or footer of its own),
  //   detail: (r) => Node | null (an extra full-width row under that row: an expanded item),
  // }) -> {setRows(rows), get selected(), clearSelection(), render()}
  function table(container, opts) {
    const o = Object.assign({ urlState: true, noun: ["item", "items"] }, opts);
    const params = new URLSearchParams(location.search);
    const state = {
      rows: o.rows || [],
      q: o.urlState ? params.get("q") || "" : "",
      chip: o.urlState ? params.get("filter") || "" : "",
      sort: o.sort ? Object.assign({}, o.sort) : null,
      selected: new Set(),
      group: o.urlState ? params.get("group") || "" : "",
    };
    if (o.urlState && params.get("sort")) {
      const s = params.get("sort");
      state.sort = { key: s.replace(/^-/, ""), dir: s.startsWith("-") ? -1 : 1 };
    }
    const keyOf = o.key || ((r) => r.id);
    const filterInput = el("input", { type: "search", placeholder: o.placeholder || "Filter", "aria-label": o.placeholder || "Filter", value: state.q });
    const chipBox = el("div", { class: "chips", style: { display: "contents" } });
    let groupSel = null;
    if (o.groupBy && o.groupBy.length) {
      groupSel = el("select", { "aria-label": "Group by" }, el("option", { value: "" }, "Nothing"), o.groupBy.map((g) => el("option", { value: g.key }, g.label)));
      groupSel.value = state.group;
      groupSel.addEventListener("change", () => { state.group = groupSel.value; writeUrl(); render(); });
    }
    const toolbar = el("div", { class: "dt-toolbar" }, el("label", { class: "dt-filter" }, el("span", { "aria-hidden": "true", class: "faint" }, "⌕"), filterInput), chipBox,
      el("div", { style: { flex: "1" } }), o.toolbarExtra || null,
      groupSel ? el("label", { class: "inline" }, "Group by", groupSel) : null);
    const thead = el("thead"); const tbody = el("tbody");
    const tableEl = el("table", { class: "dt" }, thead, tbody);
    const foot = el("div", { class: "dt-foot" });
    // plain: the table sits inside a card already (no second card, no filter box).
    const card = el("div", { class: o.plain ? "dt-plain" : "card flush" }, el("div", { class: "dt-scroll" }, tableEl), o.plain ? null : foot);
    if (o.plain) toolbar.hidden = true;
    const bulkbar = el("div", { class: "bulkbar", hidden: true, role: "region", "aria-label": "Selected rows" });
    clear(container).append(toolbar, card, bulkbar);

    function writeUrl() {
      if (!o.urlState) return;
      const u = new URL(location.href);
      state.q ? u.searchParams.set("q", state.q) : u.searchParams.delete("q");
      state.chip ? u.searchParams.set("filter", state.chip) : u.searchParams.delete("filter");
      state.group ? u.searchParams.set("group", state.group) : u.searchParams.delete("group");
      if (state.sort && (!o.sort || state.sort.key !== o.sort.key || state.sort.dir !== o.sort.dir)) u.searchParams.set("sort", (state.sort.dir < 0 ? "-" : "") + state.sort.key);
      else u.searchParams.delete("sort");
      history.replaceState(history.state, "", u);
    }
    filterInput.addEventListener("input", () => { state.q = filterInput.value; writeUrl(); render(); });

    function visible() {
      const q = state.q.trim().toLowerCase();
      const chip = (o.chips || []).find((c) => c.key === state.chip);
      let rows = state.rows.filter((r) => (!chip || chip.test(r)) &&
        (!q || (o.search ? o.search(r) : JSON.stringify(r)).toLowerCase().includes(q)));
      if (state.sort) {
        const col = o.columns.find((c) => c.key === state.sort.key);
        if (col) {
          const get = typeof col.sort === "function" ? col.sort : (r) => r[col.key];
          const cmp = (a, b) => {
            const x = get(a); const y = get(b);
            if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
            return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true })) * state.sort.dir;
          };
          if (o.children && !q && !chip) rows = treeSort(rows, cmp); else rows = rows.slice().sort(cmp);
        }
      }
      return rows;
    }
    // Keep sub-items under their parent when sorting an unfiltered tree.
    function treeSort(rows, cmp) {
      const byParent = new Map(); const keys = new Set(rows.map(keyOf));
      for (const r of rows) {
        const p = o.children(r); const k = p != null && keys.has(p) ? p : null;
        if (!byParent.has(k)) byParent.set(k, []); byParent.get(k).push(r);
      }
      const out = [];
      const walk = (k) => { for (const r of (byParent.get(k) || []).sort(cmp)) { out.push(r); walk(keyOf(r)); } };
      walk(null);
      return out;
    }

    function renderHead() {
      clear(thead);
      const tr = el("tr");
      if (o.bulk) {
        const all = el("input", { type: "checkbox", "aria-label": "Select all shown" });
        all.addEventListener("change", () => {
          for (const r of visible()) all.checked ? state.selected.add(keyOf(r)) : state.selected.delete(keyOf(r));
          render();
        });
        tr.append(el("th", { class: "sel" }, all));
      }
      for (const c of o.columns) {
        const th = el("th", { class: (c.cls || "") + (c.sort ? " sortable" : ""), scope: "col" }, c.label);
        if (c.sort) {
          const on = state.sort && state.sort.key === c.key;
          th.append(el("span", { class: "arrow", "aria-hidden": "true" }, on ? (state.sort.dir > 0 ? "▲" : "▼") : "▲▼"));
          if (on) th.setAttribute("aria-sort", state.sort.dir > 0 ? "ascending" : "descending");
          th.tabIndex = 0;
          const flip = () => {
            state.sort = on ? { key: c.key, dir: -state.sort.dir } : { key: c.key, dir: 1 };
            writeUrl(); render();
          };
          th.addEventListener("click", flip);
          th.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); flip(); } });
        }
        tr.append(th);
      }
      if (o.menu) tr.append(el("th", { class: "menu-cell" }, el("span", { class: "sr-only", hidden: true }, "Actions")));
      thead.append(tr);
      return tr;
    }
    function renderChips() {
      clear(chipBox);
      for (const c of o.chips || []) {
        const n = state.rows.filter(c.test).length;
        if (!n && c.hideEmpty !== false && state.chip !== c.key) continue;
        chipBox.append(el("button", {
          type: "button", class: "chip", "aria-pressed": String(state.chip === c.key),
          onclick: () => { state.chip = state.chip === c.key ? "" : c.key; writeUrl(); render(); },
        }, c.label, " ", el("span", { class: "n" }, n)));
      }
    }
    function render() {
      const rows = visible();
      renderChips();
      const head = renderHead();
      if (o.bulk) {
        const all = head.querySelector("input");
        const shownSel = rows.filter((r) => state.selected.has(keyOf(r))).length;
        all.checked = rows.length > 0 && shownSel === rows.length;
        all.indeterminate = shownSel > 0 && shownSel < rows.length;
      }
      clear(tbody);
      const span = o.columns.length + (o.bulk ? 1 : 0) + (o.menu ? 1 : 0);
      const grouper = (o.groupBy || []).find((g) => g.key === state.group);
      let ordered = rows;
      const heads = new Map();
      if (grouper) {
        const names = new Map();
        for (const r of rows) { const g = grouper.of(r) || ""; if (!names.has(g)) names.set(g, []); names.get(g).push(r); }
        const keys = Array.from(names.keys()).sort((a, b) => (a === "") - (b === "") || a.localeCompare(b));
        ordered = [];
        for (const k of keys) { heads.set(names.get(k)[0], [k || grouper.none || "None", names.get(k).length]); ordered.push(...names.get(k)); }
      }
      for (const r of ordered) {
        if (heads.has(r)) {
          const [name, n] = heads.get(r);
          tbody.append(el("tr", { class: "group-row" }, el("td", { colspan: span }, name, " ", el("span", { class: "faint" }, n))));
        }
        const k = keyOf(r);
        const tr = el("tr", { class: (state.selected.has(k) ? "selected " : "") + (o.rowClass ? o.rowClass(r) || "" : "") });
        if (o.bulk) {
          const cb = el("input", { type: "checkbox", "aria-label": "Select" });
          cb.checked = state.selected.has(k);
          cb.addEventListener("change", () => { cb.checked ? state.selected.add(k) : state.selected.delete(k); render(); });
          tr.append(el("td", { class: "sel" }, cb));
        }
        o.columns.forEach((c, i) => {
          const v = c.render ? c.render(r) : r[c.key];
          const empty = v == null || v === "";
          tr.append(el("td", {
            class: [c.cls, i === 0 ? "first" : null, empty ? "empty-cell" : null].filter(Boolean).join(" ") || null,
            "data-label": i === 0 || c.phone === false ? null : (c.phone || c.label),
          }, empty ? "" : v));
        });
        if (o.menu) tr.append(el("td", { class: "menu-cell" }, menuButton(() => o.menu(r), "Actions for " + (o.name ? o.name(r) : k))));
        tbody.append(tr);
        const more = o.detail ? o.detail(r) : null;
        if (more) { tr.classList.add("expanded"); tbody.append(el("tr", { class: "detail-row" }, el("td", { colspan: span }, more))); }
      }
      if (!rows.length) {
        const filtered = state.q || state.chip;
        const e = filtered
          ? { title: "Nothing matches", text: "No " + o.noun[1] + " match this filter.", action: el("button", { type: "button", class: "secondary small", onclick: () => { state.q = ""; state.chip = ""; filterInput.value = ""; writeUrl(); render(); } }, "Clear the filter") }
          : (o.empty || { title: "No " + o.noun[1] + " yet" });
        tbody.append(el("tr", {}, el("td", { colspan: span, class: "dt-empty" }, el("strong", {}, e.title), e.text ? el("div", {}, e.text) : null, e.action ? el("div", { style: { marginTop: "10px" } }, e.action) : null)));
      }
      clear(foot).append(
        el("span", {}, rows.length === state.rows.length ? state.rows.length + " " + (state.rows.length === 1 ? o.noun[0] : o.noun[1])
          : rows.length + " of " + state.rows.length + " " + o.noun[1] + " shown"),
        o.age || "");
      renderBulk();
    }
    function renderBulk() {
      if (!o.bulk) return;
      const sel = state.rows.filter((r) => state.selected.has(keyOf(r)));
      bulkbar.hidden = !sel.length;
      if (!sel.length) return;
      clear(bulkbar).append(el("span", { class: "what" }, sel.length + " selected"));
      for (const b of o.bulk(sel)) {
        bulkbar.append(el("button", { type: "button", class: b.danger ? "danger small" : "secondary small", onclick: () => b.onClick(sel) }, b.label));
      }
      bulkbar.append(el("button", { type: "button", class: "ghost small", onclick: () => { state.selected.clear(); render(); } }, "Clear"));
    }
    render();
    return {
      setRows(rows) {
        state.rows = rows || [];
        const keys = new Set(state.rows.map(keyOf));
        for (const k of Array.from(state.selected)) if (!keys.has(k)) state.selected.delete(k);
        render();
      },
      get rows() { return state.rows; },
      get selected() { return state.rows.filter((r) => state.selected.has(keyOf(r))); },
      clearSelection() { state.selected.clear(); render(); },
      render,
    };
  }

  window.UI = { $, $$, el, append, clear, fill, pill, api, fmt, menu, menuButton, confirm, drawer, tabs, region, table, closeMenu };
})();
