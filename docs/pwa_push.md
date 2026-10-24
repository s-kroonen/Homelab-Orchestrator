# The app (PWA) and push notifications

The dashboard is an installable web app. Install it on a phone or computer, turn on
notifications on **Notifications -> This device**, and alerts arrive as native
notifications even when the dashboard is closed.

## What you need

* The dashboard reached over **https** (your Traefik address). Browsers only allow push
  from a secure origin; `http://192.168.x.x:8080` cannot do it. `http://localhost` works
  for trying it out on the same machine.
* **iPhone / iPad (iOS 16.4+):** Share -> *Add to Home Screen*, open the app from there,
  then turn notifications on. Safari tabs cannot receive push.
* `NOTIFIER_ADAPTER=auto` in `.env` (or `push`) so alerts are sent to channels at all
  (the default `null` only logs them). `DRY_RUN=true` also logs only.
* Outbound HTTPS from the orchestrator container to the browsers' push services
  (Google FCM, Mozilla, Apple). Nothing else to configure: the VAPID key pair that
  identifies the orchestrator is created on first use and kept in the database
  (`Setting` key `push.vapid`, included in the System page backups - treat a backup like
  a secret). `PUSH_VAPID_SUBJECT` (optional) is the contact the push services see; it
  defaults to the dashboard's https address.

## Testing it

1. **Show a test notification here** - a notification from this browser alone (no server,
   no push service). Proves the device will display notifications; if nothing appears, the
   OS/browser notification settings are blocking it.
2. **Turn on notifications on this device**, name it.
3. **Send a test through the server** - the server encrypts a real push to this device and
   posts it to the push service, then shows what the service answered. Proves the whole
   path. A device the push service reports as gone (410/404) is removed automatically;
   other failures stay on the device's row as its *last result*.

Real alerts use the same path: the `push` channel appears next to ntfy / e-mail / webhook
in **Notifications -> Channels**, and rules can send an event to it (or leave channels
empty = all of them). Errors and critical alerts stay on screen until dismissed. With no
device registered the channel shows `skipped: no device...` in the feed - not an error.

## What is cached

Nothing. The service worker (`/sw.js`) does not cache pages or API responses - a stale
admin page is worse than an offline notice. When the orchestrator is unreachable the app
shows a small "not reachable" page.

## Files

`static/sw.js` (push + click handling), `static/pwa.js` (registration, install prompt,
subscribe), `/manifest.webmanifest`, `static/icons/`, `notify/push.py` (VAPID + delivery),
`web/routers/push.py` (manifest, `/sw.js`, `/api/push/*`).
