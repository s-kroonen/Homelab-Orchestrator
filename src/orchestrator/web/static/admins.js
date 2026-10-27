/* Admins & devices (templates/admins.html): your passkeys and invite links. */
(function () {
  "use strict";
  const { $, el, clear, api, fmt, region, drawer, confirm } = window.UI;

  const devices = region($("#devices"), {
    load: () => api.get("/api/auth/credentials", { suppressToast: true }),
    render: (creds, box) => {
      clear(box);
      if (!creds.length) { box.append(el("p", { class: "muted" }, "No devices yet.")); return; }
      box.append(el("table", { class: "dt" }, el("tbody", {}, creds.map((c) => el("tr", {},
        el("td", { class: "first" }, el("strong", {}, c.label || "(no name)")),
        el("td", { "data-label": "Added" }, el("span", { class: "faint" }, "added "), fmt.time(c.created_at)),
        el("td", { "data-label": "Last used" }, c.last_used_at ? [el("span", { class: "faint" }, "last used "), fmt.ago(c.last_used_at)] : el("span", { class: "faint" }, "never used")))))));
    },
    skeletonRows: 2,
  });

  const invites = region($("#invites"), {
    load: () => api.get("/api/auth/invites", { suppressToast: true }),
    render: (list, box) => {
      clear(box);
      if (!list.length) { box.append(el("p", { class: "muted", style: { margin: 0 } }, "No invites yet.")); return; }
      box.append(el("ul", { style: { listStyle: "none", padding: 0, margin: 0 } }, list.map((inv) => el("li", { style: { padding: "8px 0", borderTop: "1px solid var(--line)" } },
        el("div", {}, el("strong", {}, inv.label || "(no label)"), " ",
          el("span", { class: "pill " + ({ approved: "ok", pending: "pending", expired: "off", used: "ok", rejected: "fail" }[inv.status] || "off") }, inv.status)),
        el("div", { class: "faint", style: { fontSize: "12.5px" } }, "by " + inv.requested_by + ", " + fmt.ago(inv.requested_at) + (inv.expires_at ? " · expires " + fmt.time(inv.expires_at) : ""))))));
    },
    skeletonRows: 2,
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
      window.toast("Device added", "ok");
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
        }).then((copy) => {
          if (!copy) return;
          (navigator.clipboard ? navigator.clipboard.writeText(data.url) : Promise.reject())
            .then(() => window.toast("Link copied", "ok"), () => { link.select(); window.toast("Select the link and copy it", "error"); });
        });
        link.select();
      }, 0);
    },
  }));
})();
