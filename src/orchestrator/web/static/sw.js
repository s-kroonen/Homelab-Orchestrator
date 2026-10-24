/* Service worker: makes the dashboard installable and shows Web Push alerts.
 *
 * Served from /sw.js (root scope) by routers/push.py. It deliberately does NOT cache
 * pages or API responses: the dashboard must always show live state, and a stale
 * cached admin page is worse than an offline notice. */
"use strict";

const ICON = "/static/icons/icon-192.png";

const OFFLINE = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offline</title><body style="font-family:system-ui;background:#0b0e14;color:#e6e9ef;display:grid;place-items:center;min-height:100vh;margin:0">
<div style="max-width:24rem;padding:1.5rem"><h1 style="font-size:1.1rem">The orchestrator is not reachable</h1>
<p style="color:#9aa4b2">Check that the container is running and that you are on the home network or VPN, then reload.</p>
<button onclick="location.reload()" style="padding:.5rem 1rem">Reload</button></div>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(
    fetch(event.request).catch(
      () => new Response(OFFLINE, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } })
    )
  );
});

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; }
  catch (_e) { data = { title: "Orchestrator", body: event.data ? event.data.text() : "" }; }
  const urgent = data.severity === "error" || data.severity === "critical";
  event.waitUntil(
    self.registration.showNotification(data.title || "Orchestrator", {
      body: data.body || "",
      icon: ICON,
      badge: ICON,
      tag: data.tag || undefined,
      renotify: Boolean(data.tag),
      requireInteraction: urgent,  // stays until dismissed for errors
      data: { url: data.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      for (const w of windows) {
        if (new URL(w.url).origin === self.location.origin && "focus" in w) {
          return w.focus().then((c) => (c && "navigate" in c ? c.navigate(target) : c));
        }
      }
      return self.clients.openWindow(target);
    })
  );
});
