/* PWA glue: registers the service worker, offers "install", and turns Web Push on/off
 * for this device. Loaded on every page (base.html); the controls live on /notifications.
 *
 *   OrchPush.status()        -> {supported, secure, permission, subscribed, installed, canInstall, ...}
 *   OrchPush.enable(label)   ask permission, subscribe, tell the server
 *   OrchPush.disable()       unsubscribe here and on the server
 *   OrchPush.testLocal()     a notification from THIS browser (no server, no push service)
 *   OrchPush.install()       the browser's "install app" prompt, when it offers one
 */
(function () {
  "use strict";

  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const secure = window.isSecureContext;
  let registration = null;
  let installEvent = null;

  const ready = (async () => {
    if (!("serviceWorker" in navigator) || !secure) return null;
    try {
      registration = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
      return registration;
    } catch (_e) { return null; }
  })();

  window.addEventListener("beforeinstallprompt", (ev) => {
    ev.preventDefault();
    installEvent = ev;
    document.dispatchEvent(new CustomEvent("orch:installable"));
  });
  window.addEventListener("appinstalled", () => {
    installEvent = null;
    document.dispatchEvent(new CustomEvent("orch:installed"));
  });

  const standalone = () =>
    window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;

  function b64ToBytes(b64) {
    const pad = "=".repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }
  function bytesToB64(buf) {
    if (!buf) return "";
    return btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  async function currentSubscription() {
    const reg = await ready;
    return reg ? reg.pushManager.getSubscription() : null;
  }

  async function status() {
    const sub = supported && secure ? await currentSubscription() : null;
    const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
    return {
      supported,
      secure,
      permission: "Notification" in window ? Notification.permission : "unsupported",
      subscribed: Boolean(sub),
      installed: standalone(),
      canInstall: Boolean(installEvent),
      // iOS only delivers push to an app added to the Home Screen
      needsHomeScreen: ios && !standalone(),
      endpoint: sub ? sub.endpoint : null,
    };
  }

  async function enable(label) {
    if (!supported) throw new Error("This browser cannot receive push notifications.");
    if (!secure) throw new Error("Notifications need https (or localhost). Open the dashboard through its https address.");
    const reg = await ready;
    if (!reg) throw new Error("The service worker could not start.");
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      throw new Error(permission === "denied"
        ? "Notifications are blocked for this site: allow them in the browser's site settings."
        : "Permission was not given.");
    }
    const { public_key } = await apiFetch("GET", "/api/push/key");
    let sub = await reg.pushManager.getSubscription();
    if (sub) {
      // a subscription made with a different key (e.g. the DB was restored) is useless
      const key = bytesToB64(sub.options && sub.options.applicationServerKey);
      if (key && key !== public_key) { await sub.unsubscribe(); sub = null; }
    }
    if (!sub) {
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(public_key) });
    }
    const json = sub.toJSON();
    const device = label || guessLabel();
    await apiFetch("POST", "/api/push/subscribe", { endpoint: json.endpoint, keys: json.keys, label: device });
    return device;
  }

  async function disable() {
    const sub = await currentSubscription();
    if (!sub) return;
    const endpoint = sub.endpoint;
    await sub.unsubscribe();
    await apiFetch("POST", "/api/push/unsubscribe", { endpoint });
  }

  async function testLocal() {
    const reg = await ready;
    if (!reg) throw new Error("The service worker is not running (needs https or localhost).");
    if (Notification.permission !== "granted") {
      const p = await Notification.requestPermission();
      if (p !== "granted") throw new Error("Notifications are not allowed for this site.");
    }
    await reg.showNotification("Test from this browser", {
      body: "If you can read this, this device can show notifications. (Nothing was sent by the server.)",
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
      tag: "local-test",
      data: { url: "/notifications" },
    });
  }

  async function install() {
    if (!installEvent) throw new Error("The browser is not offering to install this app right now.");
    installEvent.prompt();
    const choice = await installEvent.userChoice;
    installEvent = null;
    return choice.outcome;
  }

  function guessLabel() {
    const ua = navigator.userAgent;
    const os = /android/i.test(ua) ? "Android" : /iphone|ipad/i.test(ua) ? "iPhone/iPad"
      : /windows/i.test(ua) ? "Windows" : /mac os/i.test(ua) ? "Mac" : /linux/i.test(ua) ? "Linux" : "device";
    const br = /edg\//i.test(ua) ? "Edge" : /firefox/i.test(ua) ? "Firefox" : /chrome/i.test(ua) ? "Chrome"
      : /safari/i.test(ua) ? "Safari" : "browser";
    return `${br} on ${os}`;
  }

  window.OrchPush = { status, enable, disable, testLocal, install, ready, guessLabel };
})();
