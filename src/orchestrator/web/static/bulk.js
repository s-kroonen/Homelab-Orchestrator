/* Shared bulk-selection helper: a checkbox column + an action bar.
 *
 * Used by the dashboard (services, nodes), proxy hosts and guests pages so
 * all four behave the same:
 *
 *   const bulk = Bulk.attach({
 *     key: "services",                       // sessionStorage key for results
 *     tables: [tableEl, ...],                // tables whose rows are selectable
 *     barHost: el,                           // the bar is inserted before this
 *     noun: "service",
 *     actions: [
 *       { label: "Disable", action: "disable", run: Bulk.viaApi("/api/bulk/services"),
 *         confirm: true, cls: "danger",
 *         inputs: [{ key: "group", kind: "text", placeholder: "group" }] },
 *     ],
 *     onDone: (results) => ...,              // refresh the page's data
 *   });
 *   bulk.decorate();                         // after (re)rendering rows
 *
 * A row takes part when it has a checkbox `input.bulk-cb[data-bulk-id]`
 * (server-rendered) or `tr[data-bulk-id]` (decorate() then adds the cell).
 * "Select all" only selects rows that are visible - not hidden by the page's
 * filter box - and only in its own table.
 *
 * An action's `run(ids, action, params)` returns `[{id, ok, message}]`.
 * Results always come back per item: a toast with the counts, and an inline
 * list under the bar (kept across a reload via sessionStorage, so pages that
 * reload to show the change still show what happened). Failed items stay
 * selected so a retry is one click.
 */
(function () {
  "use strict";

  (function loadCss() {
    if (document.querySelector('link[data-bulk-css]')) return;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = "/static/bulk.css";
    link.dataset.bulkCss = "1";
    document.head.appendChild(link);
  })();

  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    Object.assign(node, props || {});
    for (const k of kids) if (k != null) node.append(k);
    return node;
  }

  function isVisible(tr) {
    if (tr.hidden) return false;
    return !tr.closest("[hidden]");
  }

  function errorText(err) {
    return (err && err.message) || String(err);
  }

  /** run() for a bulk API endpoint: POST {ids, action, params}. A whole-request
   *  failure (validation, network) marks every item failed with that message. */
  function viaApi(url) {
    return async (ids, action, params) => {
      try {
        const body = await apiFetch("POST", url, { ids, action, params: params || {} }, { suppressToast: true });
        return body.results;
      } catch (err) {
        return ids.map((id) => ({ id, ok: false, message: errorText(err) }));
      }
    };
  }

  /** run() that calls `fn(id, params)` per item, at most `concurrency` at once. */
  function perItem(fn, concurrency) {
    const limit = Math.max(1, concurrency || 2);
    return async (ids, action, params) => {
      const results = new Array(ids.length);
      let next = 0;
      async function worker() {
        while (next < ids.length) {
          const i = next++;
          try {
            const message = await fn(ids[i], params || {}, action);
            results[i] = { id: ids[i], ok: true, message: message || "ok" };
          } catch (err) {
            results[i] = { id: ids[i], ok: false, message: errorText(err) };
          }
        }
      }
      await Promise.all(Array.from({ length: Math.min(limit, ids.length) }, worker));
      return results;
    };
  }

  function attach(opts) {
    const selected = new Map(); // id -> label
    const tables = opts.tables.filter(Boolean);
    const noun = opts.noun || "item";
    const storeKey = "bulk-results:" + (opts.key || noun);

    // --- the bar -----------------------------------------------------------
    const count = el("strong", { className: "bulk-count" });
    const actionsBox = el("div", { className: "bulk-actions" });
    const clearBtn = el("button", { className: "secondary small", type: "button", textContent: "Clear" });
    const status = el("span", { className: "msg bulk-status" });
    const bar = el("div", { className: "bulk-bar", hidden: true, role: "region" }, count, actionsBox, clearBtn, status);
    bar.setAttribute("aria-label", "Bulk actions");
    const resultsBox = el("div", { className: "bulk-results", hidden: true });
    opts.barHost.before(bar);
    bar.after(resultsBox);

    const buttons = [];
    for (const def of opts.actions) {
      const group = el("span", { className: "bulk-action" });
      const inputs = {};
      for (const inp of def.inputs || []) {
        let control;
        if (inp.kind === "select") {
          control = el("select", { className: "small" });
          for (const o of inp.options || []) control.append(el("option", { value: o.value, textContent: o.label }));
        } else if (inp.kind === "checkbox") {
          control = el("input", { type: "checkbox" });
        } else {
          control = el("input", { type: "text", placeholder: inp.placeholder || inp.key, size: inp.size || 10 });
        }
        control.setAttribute("aria-label", inp.label || inp.key);
        control.dataset.key = inp.key;
        inputs[inp.key] = { control, inp };
        if (inp.kind === "checkbox") group.append(el("label", { className: "inline" }, control, " " + (inp.label || inp.key)));
        else group.append(control);
      }
      const b = el("button", { type: "button", className: "small " + (def.cls || "secondary"), textContent: def.label });
      b.addEventListener("click", () => runAction(def, inputs));
      buttons.push(b);
      group.append(b);
      actionsBox.append(group);
    }
    clearBtn.addEventListener("click", () => { selected.clear(); sync(); });

    function params(inputs) {
      const out = {};
      for (const [key, { control, inp }] of Object.entries(inputs)) {
        if (inp.kind === "checkbox") out[key] = control.checked;
        else if (inp.kind === "select" && inp.number) out[key] = control.value === "" ? null : Number(control.value);
        else out[key] = control.value.trim();
      }
      return out;
    }

    async function runAction(def, inputs) {
      const ids = [...selected.keys()];
      if (!ids.length) return;
      const p = Object.assign({}, def.params || {}, params(inputs));
      const labels = ids.map((id) => selected.get(id) || String(id));
      if (def.confirm) {
        const head = typeof def.confirm === "function"
          ? def.confirm(ids, p)
          : `${def.label} ${ids.length} ${noun}(s)?`;
        const shown = labels.slice(0, 25).map((l) => "  • " + l).join("\n");
        const more = labels.length > 25 ? `\n  … and ${labels.length - 25} more` : "";
        if (!confirm(`${head}\n\n${shown}${more}`)) return;
      }
      for (const b of buttons) b.disabled = true;
      clearBtn.disabled = true;
      status.textContent = `${def.label}: running on ${ids.length} ${noun}(s)…`;
      status.className = "msg bulk-status";
      let results;
      try {
        results = await def.run(ids, def.action, p);
      } catch (err) {
        results = ids.map((id) => ({ id, ok: false, message: errorText(err) }));
      }
      for (const b of buttons) b.disabled = false;
      clearBtn.disabled = false;
      const report = {
        label: def.label,
        at: new Date().toLocaleTimeString(),
        results: results.map((r) => ({ ...r, label: selected.get(r.id) || selected.get(String(r.id)) || String(r.id) })),
      };
      // Failed items stay selected for a retry; done ones drop out.
      for (const r of results) {
        if (r.ok) { selected.delete(r.id); selected.delete(String(r.id)); }
      }
      showReport(report, true);
      try { sessionStorage.setItem(storeKey, JSON.stringify(report)); } catch (_e) { /* storage off */ }
      sync();
      if (opts.onDone) await opts.onDone(results, def);
    }

    function showReport(report, withToast) {
      const ok = report.results.filter((r) => r.ok).length;
      const failed = report.results.length - ok;
      const summary = `${report.label}: ${ok} ok` + (failed ? `, ${failed} failed` : "");
      status.textContent = "";
      resultsBox.hidden = false;
      resultsBox.replaceChildren();
      const head = el("div", { className: "bulk-results-head" },
        el("span", { className: "badge " + (failed ? (ok ? "warn" : "failed") : "healthy"), textContent: summary }),
        el("span", { className: "msg", textContent: " " + report.at + " " }),
      );
      const dismiss = el("button", { type: "button", className: "secondary small", textContent: "Dismiss" });
      dismiss.addEventListener("click", () => {
        resultsBox.hidden = true;
        try { sessionStorage.removeItem(storeKey); } catch (_e) { /* storage off */ }
      });
      head.append(dismiss);
      const list = el("ul", { className: "bulk-result-list" });
      for (const r of report.results) {
        list.append(el("li", { className: r.ok ? "ok" : "failed" },
          el("span", { className: "bulk-mark", textContent: r.ok ? "✓" : "✗" }),
          el("strong", { textContent: r.label }),
          el("span", { className: "msg", textContent: " — " + (r.message || (r.ok ? "ok" : "failed")) }),
        ));
      }
      resultsBox.append(head, list);
      if (withToast && window.toast) {
        const firstFail = report.results.find((r) => !r.ok);
        toast(summary + (firstFail ? ` (${firstFail.label}: ${firstFail.message})` : ""),
          failed ? "error" : "ok", { timeoutMs: failed ? 10000 : 5000 });
      }
    }

    // --- checkboxes ----------------------------------------------------------
    function rowsOf(table) {
      return [...table.querySelectorAll("tbody tr")].filter((tr) => tr.querySelector("input.bulk-cb"));
    }

    function headerBox(table) {
      const headRow = table.querySelector("thead tr");
      if (!headRow) return null;
      let th = headRow.querySelector("th.bulk-col");
      if (!th) {
        th = el("th", { className: "bulk-col" });
        headRow.prepend(th);
      }
      let cb = th.querySelector("input");
      if (!cb) {
        cb = el("input", { type: "checkbox", title: "Select all visible" });
        cb.setAttribute("aria-label", "Select all visible " + noun + "s");
        cb.addEventListener("change", () => {
          for (const tr of rowsOf(table)) {
            if (!isVisible(tr)) continue;
            const box = tr.querySelector("input.bulk-cb");
            if (cb.checked) selected.set(box.dataset.bulkId, box.dataset.bulkLabel || box.dataset.bulkId);
            else selected.delete(box.dataset.bulkId);
          }
          sync();
        });
        th.append(cb);
      }
      return cb;
    }

    function decorate() {
      const present = new Set();
      for (const table of tables) {
        headerBox(table);
        for (const tr of table.querySelectorAll("tbody tr")) {
          let box = tr.querySelector("input.bulk-cb");
          if (!box && tr.dataset.bulkId !== undefined) {
            box = el("input", { type: "checkbox", className: "bulk-cb" });
            box.dataset.bulkId = tr.dataset.bulkId;
            box.dataset.bulkLabel = tr.dataset.bulkLabel || tr.dataset.bulkId;
            tr.prepend(el("td", { className: "bulk-col" }, box));
          } else if (!box) {
            // A message row ("Loading…", "No nodes yet."): widen it by one.
            const only = tr.children.length === 1 ? tr.children[0] : null;
            if (only && only.colSpan && !only.dataset.bulkWidened) {
              only.colSpan += 1;
              only.dataset.bulkWidened = "1";
            }
            continue;
          }
          box.setAttribute("aria-label", "Select " + (box.dataset.bulkLabel || box.dataset.bulkId));
          present.add(box.dataset.bulkId);
          if (!box.dataset.bulkWired) {
            box.dataset.bulkWired = "1";
            box.addEventListener("change", () => {
              if (box.checked) selected.set(box.dataset.bulkId, box.dataset.bulkLabel || box.dataset.bulkId);
              else selected.delete(box.dataset.bulkId);
              sync();
            });
          }
        }
      }
      // Rows that went away (deleted, re-rendered without them) drop out.
      for (const id of [...selected.keys()]) if (!present.has(String(id))) selected.delete(id);
      sync();
    }

    function sync() {
      for (const table of tables) {
        let visible = 0;
        let on = 0;
        for (const tr of rowsOf(table)) {
          const box = tr.querySelector("input.bulk-cb");
          box.checked = selected.has(box.dataset.bulkId);
          tr.classList.toggle("bulk-selected", box.checked);
          if (isVisible(tr)) {
            visible += 1;
            if (box.checked) on += 1;
          }
        }
        const head = headerBox(table);
        if (head) {
          head.checked = visible > 0 && on === visible;
          head.indeterminate = on > 0 && on < visible;
        }
      }
      count.textContent = `${selected.size} selected`;
      bar.hidden = selected.size === 0;
    }

    // Pages that reload to show a change still show what the last run did.
    try {
      const raw = sessionStorage.getItem(storeKey);
      if (raw) showReport(JSON.parse(raw), false);
    } catch (_e) { /* storage off or bad JSON */ }

    decorate();
    return {
      decorate,
      sync,
      selectedIds: () => [...selected.keys()],
      hasSelection: () => selected.size > 0,
    };
  }

  window.Bulk = { attach, viaApi, perItem };
})();
