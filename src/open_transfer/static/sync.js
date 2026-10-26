// Open Transfer — Trusted Sync Network settings (the owner's own device only).
//
// The page never syncs anything itself: the app's background agent does, with
// or without this window. This is where the owner turns it on, chooses what
// goes where, pauses it, removes devices and sees what happened (never what
// was copied).

import { $, api, copyText, h, icon, plural, relativeTime, state, toast } from "./lib.js";
import { confirmDialog } from "./nearby.js";

const sync = {
  status: null,
  app: null,
  loading: null,
  added: null, // the iPhone/iPad just added: its token is shown once
};

const TYPE_LABELS = { text: "Text", url: "Links" };
const EXPIRY = [
  [30, "30 seconds"],
  [120, "2 minutes"],
  [600, "10 minutes"],
  [3600, "1 hour"],
];
const ERRORS = {
  unreachable: "Couldn’t reach it",
  not_allowed: "It doesn’t sync with this device",
  off: "Sync is off there",
  paused: "Paused there",
  type: "It doesn’t take that kind of clip",
  not_paired: "It doesn’t trust this device any more",
  not_accepting: "Sync is off there",
};

function until(time) {
  if (!time) return "";
  if (time > Date.now() / 1000 + 365 * 86400) return "until you resume";
  return `until ${new Date(time * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}

async function load() {
  try {
    const [{ data: status }, { data: app }] = await Promise.all([api("/api/sync"), api("/api/app")]);
    sync.status = status;
    sync.app = app;
  } catch (err) {
    toast(err.message, { tone: "error", icon: "alert" });
  }
  render();
}

async function change(path, json, method = "PUT") {
  try {
    const { data } = await api(path, { method, json });
    if (path.startsWith("/api/app")) sync.app = data;
    else sync.status = data;
  } catch (err) {
    toast(err.message, { tone: "error", icon: "alert" });
  }
  render();
}

const setPolicy = (fields) => change("/api/sync/policy", fields);

async function setDevice(device, on) {
  if (sync.status.policy.target === "all") {
    await change(`/api/sync/devices/${device.id}`, { excluded: !on });
    return;
  }
  // "Selected devices" are the same switches as Clipboard sharing.
  try {
    const { data } = await api("/api/clipboard/settings");
    const allowed = new Set(data.settings.allowed);
    if (on) allowed.add(device.id);
    else allowed.delete(device.id);
    await api("/api/clipboard/settings", { method: "PUT", json: { allowed: [...allowed] } });
  } catch (err) {
    toast(err.message, { tone: "error", icon: "alert" });
  }
  load();
}

async function removeDevice(device) {
  const ok = await confirmDialog({
    title: `Remove ${device.name}?`,
    text: "It’s removed from every device in your network and can’t sync or send without asking again. Pair it again to bring it back.",
    confirm: "Remove",
  });
  if (ok) change(`/api/sync/devices/${device.id}`, undefined, "DELETE");
}

async function leave() {
  const ok = await confirmDialog({
    title: "Leave your sync network?",
    text: "Your other devices forget this one, and it forgets them. Pair again to rejoin.",
    confirm: "Leave",
  });
  if (!ok) return;
  try {
    const { data } = await api("/api/sync/leave", { method: "POST", json: {} });
    toast(data.missed.length ? `Left. ${plural(data.missed.length, "device")} away will forget this one when told.` : "Left your sync network.");
  } catch (err) {
    toast(err.message, { tone: "error", icon: "alert" });
  }
  load();
}

// --------------------------------------------------------- iPhone and iPad

async function addClient(event) {
  event.preventDefault();
  const input = $("#sync-client-name");
  const name = input.value.trim();
  if (!name) return input.focus();
  try {
    const { data } = await api("/api/sync/clients", { method: "POST", json: { name } });
    sync.added = data;
    input.value = "";
  } catch (err) {
    toast(err.message, { tone: "error", icon: "alert" });
  }
  load();
}

async function removeClient(client) {
  const ok = await confirmDialog({
    title: `Remove ${client.name}?`,
    text: "Its shortcuts stop working right away. Add it again to get a new key.",
    confirm: "Remove",
  });
  if (!ok) return;
  if (sync.added?.client.id === client.id) sync.added = null;
  change(`/api/sync/clients/${client.id}`, undefined, "DELETE");
}

function copyRow(label, value, id) {
  return h(
    "div",
    { class: "sync-copy-row" },
    h("span", { class: "muted" }, label),
    h("code", { id }, value),
    h("button", { class: "btn btn-plain btn-small", type: "button", onclick: () => copyText(value).then(() => toast(`${label} copied`)) }, "Copy"),
  );
}

function setupCard(added) {
  const auth = `Bearer ${added.token}`;
  return h(
    "div",
    { class: "sync-setup card", id: "sync-setup" },
    h("p", {}, h("strong", {}, `Set up ${added.client.name}`), " — this key is shown only now."),
    copyRow("Key (Authorization header)", auth, "sync-setup-key"),
    copyRow("Send address", added.push_url, "sync-setup-push"),
    copyRow("Paste address", added.latest_url, "sync-setup-latest"),
    h(
      "ol",
      { class: "sync-steps" },
      h("li", {}, "In the Shortcuts app, make ", h("strong", {}, "Send clipboard"), ": Get Clipboard → Get Contents of URL (the send address, Method POST, Header Authorization = the key, Request Body JSON: text = Clipboard) → Show Notification with its ", h("em", {}, "message"), "."),
      h("li", {}, "Make ", h("strong", {}, "Paste from my devices"), ": Get Contents of URL (the paste address, the same header) → Get Dictionary Value ", h("em", {}, "text"), " → If it has any value → Copy to Clipboard."),
      h("li", {}, "Run them from Back Tap (Settings → Accessibility → Touch → Back Tap), the Action button, a widget or the Share sheet."),
    ),
    h("p", { class: "muted" }, "iPhone and iPad can’t share their clipboard in the background, so they send and paste when you run a shortcut. The phone talks to this device on your Wi-Fi; keep its address the same (most routers can reserve it)."),
    h("button", { class: "btn btn-small", type: "button", onclick: () => ((sync.added = null), render()) }, "Done"),
  );
}

function clientRow(c) {
  const paused = c.paused_until > Date.now() / 1000;
  return h(
    "li",
    { class: "sync-device", dataset: { id: c.id } },
    h(
      "div",
      { class: "sync-device-main" },
      icon("phone"),
      h("span", { class: "sync-device-name" }, c.name),
      h("span", { class: "muted sync-device-detail" }, paused ? `Paused ${until(c.paused_until)}` : c.last_seen ? `Last used ${relativeTime(c.last_seen)}` : "Not used yet"),
    ),
    h(
      "button",
      { class: "btn btn-plain btn-small", type: "button", onclick: () => change(`/api/sync/devices/${c.id}`, { pause_minutes: paused ? 0 : 60 }) },
      paused ? "Resume" : "Pause 1 h",
    ),
    h("button", { class: "icon-btn", type: "button", "aria-label": `Remove ${c.name}`, title: "Remove", onclick: () => removeClient(c) }, icon("trash")),
  );
}

function clientsSection(s) {
  return [
    h("h3", { class: "sync-heading" }, "iPhone and iPad"),
    s.clients.length ? h("ul", { class: "sync-devices list card", id: "sync-clients" }, ...s.clients.map(clientRow)) : null,
    sync.added ? setupCard(sync.added) : null,
    h(
      "form",
      { class: "sync-add-client", onsubmit: addClient },
      h("label", { class: "visually-hidden", for: "sync-client-name" }, "Name of the iPhone or iPad"),
      h("input", { id: "sync-client-name", type: "text", maxlength: 40, placeholder: "e.g. Sam’s iPhone", autocomplete: "off" }),
      h("button", { class: "btn btn-small", type: "submit", id: "sync-client-add" }, "Add iPhone or iPad"),
    ),
  ];
}

// ------------------------------------------------------------- rendering

function statusLine(s) {
  if (!s.active) return { tone: "off", text: "Off" };
  if (s.paused) return { tone: "paused", text: `Paused ${until(s.policy.paused_until)}` };
  if (!s.clipboard_access) return { tone: "warn", text: "On, but this device can’t reach its clipboard" };
  const on = s.devices.filter((d) => d.syncing).length;
  const session = s.policy.session_until > Date.now() / 1000 && !s.policy.enabled ? ` ${until(s.policy.session_until)}` : "";
  return { tone: "on", text: on ? `On${session} · syncing with ${plural(on, "device")}` : `On${session} · no devices chosen yet` };
}

function deviceRow(d, target) {
  const detail = !d.online
    ? "Away"
    : d.last_error
      ? ERRORS[d.last_error] || "Last clip didn’t arrive"
      : d.last_sent
        ? `Last sent ${relativeTime(d.last_sent)}`
        : d.last_received
          ? `Last received ${relativeTime(d.last_received)}`
          : "Nearby";
  const on = target === "all" ? !d.excluded : d.syncing || false;
  const paused = d.paused_until > Date.now() / 1000;
  return h(
    "li",
    { class: "sync-device", dataset: { id: d.id } },
    h(
      "label",
      { class: "sync-device-main" },
      h("input", { type: "checkbox", role: "switch", checked: on, "aria-label": `Sync with ${d.name}`, onchange: (e) => setDevice(d, e.target.checked) }),
      h("span", { class: "sync-device-name" }, d.name),
      h("span", { class: "muted sync-device-detail" }, paused ? `Paused ${until(d.paused_until)}` : detail),
    ),
    h(
      "button",
      { class: "btn btn-plain btn-small", type: "button", onclick: () => change(`/api/sync/devices/${d.id}`, { pause_minutes: paused ? 0 : 60 }) },
      paused ? "Resume" : "Pause 1 h",
    ),
    h(
      "button",
      { class: "icon-btn", type: "button", "aria-label": `Remove ${d.name} from your devices`, title: "Remove from all your devices", onclick: () => removeDevice(d) },
      icon("trash"),
    ),
  );
}

function select(id, label, value, options, onchange) {
  return h(
    "label",
    { class: "sync-field" },
    h("span", {}, label),
    h(
      "select",
      { id, onchange: (e) => onchange(e.target.value) },
      ...options.map(([v, text]) => h("option", { value: String(v), selected: String(v) === String(value) }, text)),
    ),
  );
}

function render() {
  const box = $("#sync-body");
  const s = sync.status;
  if (!box || !s) return;
  const p = s.policy;
  const line = statusLine(s);
  const note = $("#clip-sync-state");
  if (note) {
    note.textContent = { on: "On", warn: "On", paused: "Paused", off: "Off" }[line.tone];
    note.dataset.tone = line.tone;
  }
  // The background agent does what the "while this window is open" box did.
  const auto = $("#clip-auto")?.closest("label");
  if (auto && s.active && s.clipboard_access) auto.hidden = true;

  const parts = [
    h(
      "div",
      { class: "sync-main" },
      h(
        "label",
        { class: "sync-switch" },
        h("input", { type: "checkbox", role: "switch", id: "sync-enabled", checked: p.enabled, onchange: (e) => setPolicy({ enabled: e.target.checked }) }),
        h("strong", {}, "Sync the clipboard on this device"),
      ),
      h("p", { class: `sync-status tone-${line.tone}`, id: "sync-status", role: "status" }, line.text),
      h(
        "div",
        { class: "sync-actions" },
        s.paused
          ? h("button", { class: "btn btn-small", type: "button", id: "sync-resume", onclick: () => change("/api/sync/resume", {}, "POST") }, "Resume")
          : s.active
            ? [
                h("button", { class: "btn btn-small", type: "button", id: "sync-pause", onclick: () => change("/api/sync/pause", { minutes: 60 }, "POST") }, "Pause 1 hour"),
                h("button", { class: "btn btn-plain btn-small", type: "button", onclick: () => change("/api/sync/pause", { minutes: null }, "POST") }, "Pause until I resume"),
              ]
            : h("button", { class: "btn btn-plain btn-small", type: "button", id: "sync-session", onclick: () => setPolicy({ session_minutes: 60 }) }, "Sync for the next hour only"),
      ),
    ),
  ];

  parts.push(
    h("h3", { class: "sync-heading" }, "Your devices"),
    h(
      "div",
      { class: "sync-target", role: "radiogroup", "aria-label": "Send to" },
      ...[
        ["all", "All my trusted devices"],
        ["selected", "Only the ones I switch on"],
      ].map(([value, text]) =>
        h("label", {}, h("input", { type: "radio", name: "sync-target", value, checked: p.target === value, onchange: () => setPolicy({ target: value }) }), ` ${text}`),
      ),
    ),
    s.devices.length
      ? h("ul", { class: "sync-devices list card", id: "sync-devices" }, ...s.devices.map((d) => deviceRow(d, p.target)))
      : h("p", { class: "muted" }, "No trusted devices yet. Pair one with Add device → Enter a code; your other devices meet it automatically."),
  );

  parts.push(...clientsSection(s));

  parts.push(
    h("h3", { class: "sync-heading" }, "What and when"),
    select("sync-mode", "Send", p.mode, [
      ["auto", "Every time I copy"],
      ["manual", "Only when I ask"],
    ], (v) => setPolicy({ mode: v })),
    h(
      "fieldset",
      { class: "sync-types" },
      h("legend", {}, "Content"),
      ...Object.entries(TYPE_LABELS).map(([type, text]) =>
        h(
          "label",
          {},
          h("input", {
            type: "checkbox",
            value: type,
            checked: p.types.includes(type),
            onchange: () => {
              const types = [...document.querySelectorAll(".sync-types input:checked")].map((i) => i.value);
              if (types.length) setPolicy({ types });
              else render(); // at least one
            },
          }),
          ` ${text}`,
        ),
      ),
    ),
    select("sync-sensitive", "Passwords, codes and keys", p.sensitive, [
      ["block", "Never sync automatically"],
      ["allow", "Sync them too"],
    ], (v) => setPolicy({ sensitive: v })),
    select("sync-expiry", "Keep a clip for a device that’s away", p.expiry, EXPIRY, (v) => setPolicy({ expiry: Number(v) })),
  );

  if (sync.app?.desktop) {
    parts.push(
      h(
        "label",
        { class: "sync-field sync-blocked" },
        h("span", {}, "Never sync copies from these apps (one per line, e.g. com.agilebits.onepassword7 or KeePass.exe)"),
        h("textarea", { id: "sync-blocked", rows: 3, onchange: (e) => setPolicy({ blocked_sources: e.target.value.split("\n").map((x) => x.trim()).filter(Boolean) }) }, p.blocked_sources.join("\n")),
      ),
    );
  }

  parts.push(
    h(
      "label",
      { class: "sync-check" },
      h("input", { type: "checkbox", checked: p.accept_introductions, onchange: (e) => setPolicy({ accept_introductions: e.target.checked }) }),
      " Let my devices introduce new devices to this one",
    ),
  );

  if (sync.app?.desktop) {
    const a = sync.app;
    parts.push(
      h("h3", { class: "sync-heading" }, "In the background"),
      h(
        "p",
        { class: "muted" },
        a.background
          ? "Open Transfer is running in the background, so closing this window keeps syncing."
          : "With sync on, closing this window keeps Open Transfer syncing in the background.",
      ),
      a.can_start_at_login
        ? h(
            "label",
            { class: "sync-check" },
            h("input", { type: "checkbox", role: "switch", id: "sync-login", checked: a.start_at_login, onchange: (e) => change("/api/app", { start_at_login: e.target.checked }) }),
            " Start at login",
          )
        : null,
      a.background
        ? h(
            "button",
            {
              class: "btn btn-plain btn-small",
              type: "button",
              id: "sync-quit",
              onclick: async () => {
                const ok = await confirmDialog({ title: "Quit Open Transfer?", text: "Sharing and clipboard sync stop until you open it again.", confirm: "Quit" });
                if (ok) api("/api/app/quit", { method: "POST", json: {} }).catch(() => {});
              },
            },
            "Quit Open Transfer",
          )
        : null,
    );
  }

  if (s.recent.length) {
    parts.push(
      h("h3", { class: "sync-heading" }, "Recently"),
      h(
        "ul",
        { class: "sync-recent", id: "sync-recent" },
        ...s.recent.slice(0, 6).map((r) =>
          h(
            "li",
            {},
            icon(r.direction === "sent" ? "upload" : "download"),
            h(
              "span",
              {},
              r.direction === "sent"
                ? r.devices.length
                  ? `Sent to ${r.devices.join(", ")}`
                  : r.queued
                    ? `Waiting for ${plural(r.queued, "device")}`
                    : "Not sent"
                : `${r.applied === false ? "Not pasted" : "Received"} from ${r.devices.join(", ")}`,
            ),
            h("span", { class: "muted" }, ` · ${TYPE_LABELS[r.type] || r.type} · ${relativeTime(r.at)}`),
          ),
        ),
      ),
    );
  }

  if (s.devices.length) {
    parts.push(h("button", { class: "btn btn-plain btn-small sync-leave", type: "button", onclick: leave }, "Leave the sync network"));
  }
  // Re-rendering mustn't lose what's being typed (or where the focus was).
  const typed = [...box.querySelectorAll("input[type=text], textarea")].map((el) => [el.id, el.value]);
  const focused = document.activeElement?.id;
  box.replaceChildren(...parts.flat().filter(Boolean));
  for (const [id, value] of typed) {
    const el = id && document.getElementById(id);
    if (el && el.value !== value && el.tagName === "INPUT") el.value = value;
  }
  if (focused) document.getElementById(focused)?.focus();
}

export function initSync() {
  const button = $("#sync-open");
  if (!button || !state.info.owner) return;
  button.addEventListener("click", async () => {
    $("#sync-dialog").showModal();
    await load();
  });
  // Keep the chip and the open dialog fresh as things change.
  window.addEventListener("ot-mesh-state", () => {
    if (sync.loading) return;
    if ($("#sync-dialog")?.open || !sync.status) {
      sync.loading = load().finally(() => {
        sync.loading = null;
      });
    }
  });
}
