/* Shared toast + fetch helper for every homelab-orchestrator page.
 *
 * Loaded on every page via base.html. Replaces the bespoke
 * sendJson/errorText/apiError-style helpers that used to be copy-pasted
 * into individual templates (dashboard.html, proxy_hosts.html,
 * service_detail.html, backup_policies.html, setup.html) — those all parsed
 * a FastAPI HTTPException body ({"detail": ...}) slightly differently, and
 * none of them showed anything for a plain network failure or an unhandled
 * promise rejection.
 *
 * toast(message, kind)
 *   Shows a small dismissible banner in the top-right corner. Auto-dismisses
 *   after a few seconds; always has a close button. No per-page markup
 *   needed — the stack container is created lazily on first use.
 *
 * apiFetch(method, url, body, opts)
 *   Sends `body` as JSON (when given), returns the parsed JSON response on
 *   success. On any non-2xx response, or on a network-level failure (fetch
 *   itself rejecting), it throws an Error whose `.message` is the backend's
 *   error text — parsed from the conventional {"detail": ...} /
 *   {"message": ...} / {"error": ...} shapes this app's routers use — and,
 *   unless `opts.suppressToast` is true, also shows that error as a toast
 *   before throwing. The thrown Error also carries `.status`, `.detail` and
 *   `.fieldErrors` ({field: message} from a structured
 *   {"detail": {"message", "errors"}} body) for inline form errors. This means a caller that does nothing special with the
 *   rejection (no try/catch, or a catch that only updates its own inline
 *   .msg element) still always shows the user *something* on failure.
 */
(function () {
  "use strict";

  function ensureStack() {
    let stack = document.getElementById("toast-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.id = "toast-stack";
      stack.setAttribute("role", "status");
      stack.setAttribute("aria-live", "polite");
      document.body.appendChild(stack);
    }
    return stack;
  }

  function toast(message, kind, opts) {
    const options = opts || {};
    const stack = ensureStack();
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    const text = document.createElement("span");
    text.className = "toast-text";
    text.textContent = message;
    const close = document.createElement("button");
    close.className = "toast-close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    el.append(text, close);
    stack.appendChild(el);

    let dismissed = false;
    const dismiss = () => {
      if (dismissed) return;
      dismissed = true;
      el.remove();
    };
    close.addEventListener("click", dismiss);
    const timeoutMs = options.timeoutMs ?? (kind === "error" ? 8000 : 4000);
    if (timeoutMs > 0) setTimeout(dismiss, timeoutMs);
    return dismiss;
  }

  // Pulls a human-readable message out of the backend's JSON error body.
  // FastAPI's HTTPException renders {"detail": "<str>"} or, for validation
  // errors, {"detail": [{"msg": ..., ...}, ...]}. A couple of routers (see
  // web/deps.py) also use {"message": ...} / {"error": ...} - support all
  // three rather than guessing which one a given endpoint uses.
  function extractMessage(data) {
    if (data == null) return null;
    if (typeof data === "string") return data;
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail.map((d) => (d && d.msg) || JSON.stringify(d)).join("; ");
    }
    if (typeof data.message === "string") return data.message;
    if (typeof data.error === "string") return data.error;
    // Structured validation error: {"detail": {"message": "...", "errors": {field: msg}}}
    if (data.detail && typeof data.detail === "object" && typeof data.detail.message === "string") {
      return data.detail.message;
    }
    if (data.detail !== undefined) return JSON.stringify(data.detail);
    return null;
  }

  // {field: message} map from a structured validation error, or {}.
  function extractFieldErrors(data) {
    const d = data && data.detail;
    if (d && typeof d === "object" && !Array.isArray(d) && d.errors && typeof d.errors === "object") {
      return d.errors;
    }
    return {};
  }

  async function responseError(resp) {
    let data = null;
    try {
      data = await resp.json();
    } catch (_err) {
      /* body wasn't JSON (or was empty) - fall through to the status line */
    }
    const message = extractMessage(data) || `${resp.status} ${resp.statusText || ""}`.trim();
    const err = new Error(message);
    // Callers that want inline per-field errors read these off the thrown error.
    err.status = resp.status;
    err.fieldErrors = extractFieldErrors(data);
    err.detail = data && data.detail !== undefined ? data.detail : null;
    return err;
  }

  async function apiFetch(method, url, body, opts) {
    const options = opts || {};
    let resp;
    try {
      resp = await fetch(url, {
        method,
        headers: body === undefined ? undefined : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (err) {
      // Network failure, CORS, DNS, offline, etc. - fetch() rejects rather
      // than resolving with a response, so this is the one path the old
      // per-page try/catches never had a chance to reach.
      const message = "Network error: " + (err && err.message ? err.message : "request failed");
      if (!options.suppressToast) toast(message, "error");
      throw new Error(message);
    }
    if (!resp.ok) {
      const err = await responseError(resp);
      if (!options.suppressToast) toast(err.message, "error");
      throw err;
    }
    if (resp.status === 204) return null;
    const contentType = resp.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) return null;
    return resp.json();
  }

  // The page the operator came from, so "Apply" (save, then go back) knows where
  // back is. Remembered per page for the tab, so a reload after "Save" keeps it.
  // `?next=` wins (a redirect after creating something carries it along); only
  // same-origin relative paths are ever used.
  const PrevPage = (function () {
    function safe(path) {
      return typeof path === "string" && path.startsWith("/") && !path.startsWith("//") ? path : null;
    }
    const key = "orch:prev:" + location.pathname;
    let prev = safe(new URLSearchParams(location.search).get("next"));
    try {
      if (!prev && document.referrer) {
        const r = new URL(document.referrer);
        if (r.origin === location.origin && r.pathname !== location.pathname) {
          prev = safe(r.pathname + r.search + r.hash);
        }
      }
      if (prev) sessionStorage.setItem(key, prev);
      else prev = safe(sessionStorage.getItem(key));
    } catch (_e) { /* storage may be unavailable */ }
    return {
      url(fallback) { return prev || fallback || "/"; },
      go(fallback) { location.href = prev || fallback || "/"; },
      // For redirects that must keep the way back: "/x/edit" -> "/x/edit?next=..."
      carry(url) { return prev ? url + (url.includes("?") ? "&" : "?") + "next=" + encodeURIComponent(prev) : url; },
    };
  })();

  window.PrevPage = PrevPage;
  window.toast = toast;
  window.apiFetch = apiFetch;
})();
