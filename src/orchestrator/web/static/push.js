/* Deferred push: the DB is the working copy, outside services (Traefik
 * Manager today) are written only on "Save to YAML" or "Push now".
 *
 * Shared by every page that has a Save button, so the outcome of the push is
 * never silent:
 *
 *   PushUI.save({ msgId, reload })
 *     POST /api/registry/save, then report the push: a success/failure toast,
 *     a line in the page's message element and - on pages with a
 *     #push-results container (proxy hosts) - one line per item. Returns
 *     { failed, results }.
 *
 *   PushUI.renderResults(container, results)
 *   PushUI.confirmText(plan)       text for the "Push now" confirm dialog
 *   PushUI.status()                GET /api/push/status -> the proxy target
 *
 * Depends on app.js (apiFetch, toast).
 */
(function () {
  "use strict";

  const ACTION_LABEL = {
    create: "created",
    update: "updated",
    enable: "enabled",
    disable: "disabled",
    delete: "deleted",
    fallback: "maintenance page",
    verify: "verified",
    connect: "connect",
  };

  function summarize(results) {
    const done = (results || []).filter((r) => !r.planned);
    const failed = done.filter((r) => !r.ok);
    return { attempted: done.length, failed: failed.length, ok: done.length - failed.length };
  }

  function renderResults(container, results, heading) {
    if (!container) return;
    container.textContent = "";
    if (!results || !results.length) {
      container.hidden = true;
      return;
    }
    container.hidden = false;
    const title = document.createElement("div");
    title.className = "push-results-title";
    const sum = summarize(results);
    title.textContent = heading || (sum.failed
      ? `Push finished with ${sum.failed} problem(s), ${sum.ok} done`
      : `Pushed ${sum.ok} change(s) to Traefik Manager`);
    container.classList.toggle("has-failures", sum.failed > 0);
    container.classList.toggle("all-ok", sum.failed === 0);
    container.appendChild(title);
    const list = document.createElement("ul");
    list.className = "push-results-list";
    for (const r of results) {
      const li = document.createElement("li");
      li.className = r.ok ? "ok" : "bad";
      const badge = document.createElement("span");
      badge.className = "badge " + (r.ok ? "healthy" : "failed");
      badge.textContent = (r.ok ? "" : "failed: ") + (ACTION_LABEL[r.action] || r.action);
      const host = document.createElement("code");
      host.textContent = r.hostname;
      const msg = document.createElement("span");
      msg.className = "push-msg";
      msg.textContent = r.message || "";
      li.append(badge, " ", host, " ", msg);
      list.appendChild(li);
    }
    container.appendChild(list);
  }

  function confirmText(plan) {
    const items = (plan || []).map((r) => `- ${r.hostname}: ${r.message || r.action}`);
    if (!items.length) return "Nothing to push.";
    return "Push these changes to Traefik Manager?\n\n" + items.join("\n");
  }

  async function status() {
    const body = await apiFetch("GET", "/api/push/status", undefined, { suppressToast: true });
    return (body.targets || []).find((t) => t.name === "proxy") || null;
  }

  async function save(opts) {
    const o = opts || {};
    const msg = o.msgId ? document.getElementById(o.msgId) : null;
    const say = (text, kind) => {
      if (!msg) return;
      msg.textContent = text;
      msg.className = "msg" + (kind ? " " + kind : "");
    };
    let body;
    try {
      body = await apiFetch("POST", "/api/registry/save");
    } catch (err) {
      say("Save failed: " + err.message, "error");
      return { failed: 1, results: [], saved: false };
    }
    const results = body.push_results || [];
    const sum = summarize(results);
    renderResults(document.getElementById("push-results"), results);
    if (sum.failed) {
      const text = `YAML saved, but ${sum.failed} push item(s) failed - see below / Push now to retry.`;
      say(text, "error");
      if (window.toast) toast(text, "error", { timeoutMs: 10000 });
    } else if (sum.attempted) {
      const text = `Saved. Pushed ${sum.ok} change(s) to Traefik Manager.`;
      say(text, "ok");
      if (window.toast) toast(text, "ok");
    } else {
      say("Saved.", "ok");
    }
    if (o.after) await o.after(sum, results);
    // A clean save reloads to clear the unsaved badge; a failed push stays put
    // so the per-item results can be read.
    if (o.reload !== false && !sum.failed) setTimeout(() => location.reload(), 900);
    return { failed: sum.failed, results, saved: true };
  }

  window.PushUI = { save, summarize, renderResults, confirmText, status };
})();
