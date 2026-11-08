/* Admins & devices (templates/admins.html), to the Admins wireframe: your passkeys (the one this
 * browser uses is marked; ⋯ renames or removes one) and the invite links, open ones with Copy
 * and Revoke.
 * Data: embedded at first paint; /api/auth/credentials and /api/auth/invites after a change. */
(function () {
  "use strict";
  const { $, el, clear, api, fmt, region, table, drawer, confirm, menuButton } = window.UI;
  const V = JSON.parse($("#initial").textContent);
  const toast = (m, k) => window.toast(m, k);

  function copy(url) {
    (navigator.clipboard ? navigator.clipboard.writeText(url) : Promise.reject())
      .then(() => toast("Link copied", "ok"), () => window.prompt("Copy the invite link", url));
  }

  function renamePasskey(c) {
    drawer({
      title: "Rename passkey",
      values: { label: c.label || "" },
      saveLabel: "Save",
      fields: [{ key: "label", label: "Name", placeholder: "Phone, laptop…", help: "So you can tell your devices apart." }],
      async onSave(v) {
        await api.patch("/api/auth/credentials/" + c.id, { label: (v.label || "").trim() });
        toast("Passkey renamed", "ok");
        devices.refresh();
      },
    });
  }

  async function removePasskey(c, count) {
    if (count <= 1) {
      toast("This is your only passkey. Add another device first, or you could not sign in again.", "error");
      return;
    }
    const ok = await confirm({
      title: "Remove " + (c.label || "this passkey") + "?",
      body: c.this_device
        ? "This is the passkey this browser signed in with. You are signed out here, and this device can no longer sign in."
        : "That device can no longer sign in, and every session it signed in ends now. Use this for a lost or stolen device.",
      action: "Remove passkey",
    });
    if (!ok) return;
    const res = await api.del("/api/auth/credentials/" + c.id);
    if (res && res.signed_out) { location.href = "/login"; return; }
    toast("Passkey removed", "ok");
    devices.refresh();
  }

  const t = table($("#devices"), {
    rows: V.credentials, key: (c) => c.id, plain: true, urlState: false, noun: ["passkey", "passkeys"],
    sort: { key: "added", dir: -1 },
    name: (c) => c.label || "passkey",
    menu: (c) => {
      const count = (devices.data || V.credentials).length;
      return [
        { label: "Rename…", onClick: () => renamePasskey(c) },
        "-",
        { label: "Remove…", danger: true, disabled: count <= 1, title: count <= 1 ? "Your only passkey: add another device first" : undefined, onClick: () => removePasskey(c, count) },
      ];
    },
    columns: [
      { key: "device", label: "Device", sort: (c) => (c.label || "").toLowerCase(),
        render: (c) => [el("strong", {}, c.label || "(no name)"), c.this_device ? [" ", el("span", { class: "pill ok" }, "this device")] : null] },
      { key: "added", label: "Added", sort: (c) => c.created_at, render: (c) => el("span", { class: "faint", title: fmt.time(c.created_at) }, fmt.short(c.created_at)) },
      { key: "used", label: "Last used", sort: (c) => c.last_used_at || "", render: (c) => c.last_used_at ? el("span", { title: fmt.time(c.last_used_at) }, fmt.ago(c.last_used_at)) : el("span", { class: "faint" }, "never") },
    ],
    empty: { title: "No passkeys yet", text: "Add this device to sign in with it." },
  });
  const devices = region($("#devices"), {
    initial: V.credentials,
    load: () => api.get("/api/auth/credentials", { suppressToast: true }),
    render: (rows) => t.setRows(rows),
    age: $("#dev-age"),
  });

  const STATUS = { pending: ["pending", "open"], consumed: ["ok", "used"], approved: ["ok", "used"], expired: ["off", "expired"], denied: ["off", "revoked"] };

  async function revoke(inv) {
    const ok = await confirm({
      title: "Revoke this invite?",
      body: "The link stops working now. Make a new one if it is still needed.",
      action: "Revoke",
    });
    if (!ok) return;
    await api.del("/api/auth/invites/" + inv.id);
    toast("Invite revoked", "ok");
    invites.refresh();
  }
  const invites = region($("#invites"), {
    initial: V.invites,
    load: () => api.get("/api/auth/invites", { suppressToast: true }),
    render: (list, box) => {
      clear(box);
      if (!list.length) { box.append(el("p", { class: "muted", style: { margin: 0 } }, "No invites yet.")); return; }
      for (const inv of list) {
        const [cls, word] = STATUS[inv.status] || ["off", inv.status];
        if (inv.url) {
          box.append(el("div", { class: "invite" },
            el("div", { class: "invite-line" }, el("span", { class: "pill " + cls }, word), el("span", { class: "mono" }, inv.url.replace(/^https?:\/\/[^/]+/, "…")),
              el("button", { type: "button", class: "secondary small", style: { marginLeft: "auto" }, onclick: () => copy(inv.url) }, "Copy"),
              menuButton([{ label: "Revoke…", danger: true, onClick: () => revoke(inv) }], "Actions for this invite")),
            el("div", { class: "faint", style: { fontSize: "12.5px", marginTop: "4px" } },
              [inv.label ? "for " + inv.label : "", "made by " + inv.requested_by + " " + fmt.ago(inv.requested_at), "expires in " + fmt.ago(inv.expires_at).replace(/ from now$/, "")].filter(Boolean).join(" · "))));
        } else {
          box.append(el("div", { class: "invite-done" }, el("span", { class: "pill " + cls }, word),
            el("span", {}, inv.approved_by ? ["by ", el("strong", {}, inv.approved_by)] : el("span", { class: "faint" }, inv.label ? "for " + inv.label : "not used")),
            el("span", { class: "faint", style: { marginLeft: "auto", fontSize: "12.5px" } }, fmt.short(inv.approved_at || inv.expires_at))));
        }
      }
    },
  });

  $("#add-device").addEventListener("click", () => drawer({
    title: "Add this device",
    sub: "Registers a passkey on the device you are using now.",
    values: { label: "" },
    saveLabel: "Add passkey",
    note: "Your browser asks to create the passkey next.",
    fields: [{ key: "label", label: "Name", placeholder: "Phone, laptop…", help: "So you can tell your devices apart. Optional." }],
    async onSave(v) {
      await window.addDevicePasskey((v.label || "").trim());
      toast("Device added", "ok");
      devices.refresh();
    },
  }));

  $("#new-invite").addEventListener("click", () => drawer({
    title: "Invite another admin",
    values: { label: "" },
    saveLabel: "Create link",
    fields: [{ key: "label", label: "Who is it for?", placeholder: "a name", help: "Shown in the list of invites. Optional." }],
    async onSave(v) {
      const data = await api.post("/api/auth/invites", { label: (v.label || "").trim() }, { suppressToast: true });
      invites.refresh();
      const link = el("input", { readonly: true, value: data.url, class: "mono", style: { width: "100%" }, "aria-label": "Invite link" });
      setTimeout(() => {
        confirm({
          title: "Send this link",
          body: el("div", {}, el("p", {}, "It works once and expires " + fmt.time(data.expires_at) + ". Anyone with it can become an admin, so send it privately."), link),
          action: "Copy link", cancel: "Close", danger: false,
        }).then((ok) => { if (ok) copy(data.url); });
        link.select();
      }, 0);
    },
  }));
})();
