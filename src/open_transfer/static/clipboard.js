// Open Transfer — clipboard: reading it to send, and the Clipboard panel on
// this device's own window (history, sharing with paired devices, auto-share).

import { copyImage } from "./media.js";
import { $, api, copyText, formatBytes, h, icon, looksSensitive, plural, relativeTime, SENSITIVE_REASONS, stamp, state, toast } from "./lib.js";
import { confirmDialog } from "./nearby.js";
import { notify } from "./notify.js";

const AUTO_MS = 1500;
const IMAGE_EXT = { "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp" };

const clip = {
  items: [],
  devices: [],
  settings: null,
  latest: null, // newest received item id we've seen (null until the first state)
  paired: -1,
  sharing: -1,
  lastText: "", // what auto-share last saw (or received), so nothing echoes back
  autoTimer: 0,
  loaded: false,
};

// --------------------------------------------------------------- reading

/**
 * What's on the clipboard: ``{ file }`` for an image, ``{ text }`` for text,
 * or null (empty, or the browser said no — the user is told why).
 */
export async function readClipboard({ quiet = false } = {}) {
  try {
    if (navigator.clipboard?.read) {
      for (const item of await navigator.clipboard.read()) {
        const type = item.types.find((t) => t in IMAGE_EXT);
        if (type) {
          const blob = await item.getType(type);
          return { file: new File([blob], `Clipboard image ${stamp()}.${IMAGE_EXT[type]}`, { type }) };
        }
      }
    }
  } catch {
    /* no image access: try text */
  }
  try {
    const text = await navigator.clipboard.readText();
    if (text.trim()) return { text };
    if (!quiet) toast("The clipboard is empty.", { icon: "copy" });
  } catch {
    if (!quiet) toast("Allow clipboard access to send what you copied.", { tone: "error", icon: "alert" });
  }
  return null;
}

const toBase64 = (blob) =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });

// ---------------------------------------------------------------- panel

async function refresh() {
  try {
    const q = $("#clip-search").value.trim();
    const { data } = await api(`/api/clipboard${q ? `?q=${encodeURIComponent(q)}` : ""}`);
    Object.assign(clip, { items: data.items, devices: data.devices, settings: data.settings, loaded: true });
    render();
  } catch {
    /* next state change tries again */
  }
}

function sharingNames() {
  return clip.devices.filter((d) => d.allowed).map((d) => d.name);
}

function render() {
  const section = $("#clipboard-section");
  section.hidden = !state.info.owner || (!clip.devices.length && !clip.items.length && !$("#clip-search").value);
  if (section.hidden) return;
  const names = sharingNames();
  $("#clip-sync-note").textContent = names.length
    ? `Sharing with ${names.join(", ")}`
    : clip.devices.length
      ? "Not sharing with any device yet — open Sync settings"
      : "Pair a device to share your clipboard with it";
  $("#clip-push").disabled = false;
  $("#clip-meta").textContent = clip.items.length ? plural(clip.items.length, "item") : "";
  $("#clip-clear").hidden = !clip.items.length;
  const list = $("#clip-list");
  list.hidden = !clip.items.length;
  list.replaceChildren(...clip.items.map(clipRow));
  if ($("#clip-dialog").open) renderSettings();
}

function clipRow(item) {
  const image = item.kind === "image";
  const thumb = h("span", { class: "thumb", "aria-hidden": "true" }, icon(image ? "image" : "doc"));
  if (image) thumb.append(h("img", { class: "is-loaded", src: `/api/clipboard/${encodeURIComponent(item.id)}/image`, alt: "" }));
  const who = item.direction === "received" ? `From ${item.device.name}` : item.device.name ? `To ${item.device.name}` : "Not sent to anyone";
  const sub = [who, relativeTime(item.at), item.transient && "Not kept — looks private"].filter(Boolean).join(" · ");
  // Private-looking text stays hidden on screen; Copy still works.
  const title = image ? `Image · ${formatBytes(item.size)}` : item.transient ? "•••••••• (hidden)" : item.text;
  return h(
    "li",
    { class: "row clip-row", dataset: { id: item.id } },
    thumb,
    h("div", { class: "row-main" }, h("span", { class: "row-title clip-text", title: image || item.transient ? "" : item.text.slice(0, 500) }, title), h("span", { class: "row-sub" }, sub)),
    h(
      "div",
      { class: "row-actions is-visible" },
      h("button", { class: "btn btn-tinted btn-small", type: "button", "aria-label": `Copy ${image ? "image" : "text"} again`, onclick: () => copyItem(item) }, "Copy"),
      h("button", { class: "icon-btn is-danger", type: "button", "aria-label": "Delete from clipboard history", title: "Delete", onclick: () => removeItem(item) }, icon("trash")),
    ),
  );
}

async function copyItem(item, { announce = true } = {}) {
  let ok;
  if (item.kind === "image") ok = await copyImage(`/api/clipboard/${encodeURIComponent(item.id)}/image`);
  else {
    clip.lastText = item.text;
    ok = await copyText(item.text);
  }
  if (announce) toast(ok ? "Copied" : "Couldn’t copy — click into the window and try again", { icon: "copy", tone: ok ? "info" : "error" });
  return ok;
}

async function removeItem(item) {
  try {
    await api(`/api/clipboard/${encodeURIComponent(item.id)}`, { method: "DELETE" });
  } catch (err) {
    toast(err.message, { tone: "error", icon: "alert" });
  }
  refresh();
}

// ---------------------------------------------------------------- sharing

/** Share the clipboard with every paired device this one shares with. */
async function share(content, { auto = false } = {}) {
  let body;
  if (content.file) {
    if (!(content.file.type in IMAGE_EXT)) return;
    body = { kind: "image", mime: content.file.type, data: await toBase64(content.file) };
  } else {
    body = { kind: "text", text: content.text };
  }
  for (;;) {
    try {
      const { data } = await api("/api/clipboard", { method: "POST", json: body, timeout: 60000 });
      report(data, auto);
      break;
    } catch (err) {
      if (err.code !== "sensitive" || auto || body.confirm_sensitive) {
        if (!auto) toast(err.message, { tone: "error", icon: "alert" });
        return;
      }
      const ok = await confirmDialog({
        title: `This looks like ${SENSITIVE_REASONS[err.detail?.reason] || "something private"}`,
        text: "It won’t be kept in clipboard history. Share it anyway?",
        confirm: "Share anyway",
      });
      if (!ok) return;
      body = { ...body, confirm_sensitive: true };
    }
  }
  refresh();
}

function report(result, auto) {
  const delivered = result.delivered.map((d) => d.name);
  if (delivered.length) {
    toast(`Shared with ${delivered.join(", ")}`, { tone: "success", icon: "check" });
  } else if (!auto && result.failed.length) {
    toast(`Not shared: ${result.failed.map((f) => `${f.name} — ${f.reason}`).join("; ")}`, { tone: "error", icon: "alert", duration: 6000 });
  } else if (!auto) {
    toast("Saved here. Turn on sharing for a paired device in Sync settings to send it too.", { icon: "copy", duration: 5000 });
  }
}

async function pushClipboard() {
  const content = await readClipboard();
  if (!content) return;
  if (content.text) clip.lastText = content.text;
  await share(content);
}

// ----------------------------------------------------------- auto-share

function autoEnabled() {
  try {
    return localStorage.getItem("ot-clip-auto") === "1";
  } catch {
    return false;
  }
}

function setAuto(on) {
  try {
    localStorage.setItem("ot-clip-auto", on ? "1" : "0");
  } catch {
    /* lasts until the page closes */
  }
  clearInterval(clip.autoTimer);
  clip.autoTimer = 0;
  if (!on) return;
  // Start from what's there now: only what's copied from here on is shared.
  readClipboard({ quiet: true }).then((content) => {
    clip.lastText = content?.text || "";
  });
  clip.autoTimer = setInterval(autoTick, AUTO_MS);
}

let ticking = false;
async function autoTick() {
  // Browsers only let a page read the clipboard while it has focus.
  if (ticking || document.hidden || !document.hasFocus() || !sharingNames().length) return;
  ticking = true;
  try {
    const text = await navigator.clipboard.readText();
    if (text.trim() && text !== clip.lastText) {
      clip.lastText = text;
      if (looksSensitive(text)) toast("Didn’t share what you copied: it looks private. Use Share clipboard to send it.", { icon: "lock", duration: 5000 });
      else await share({ text }, { auto: true });
    }
  } catch {
    $("#clip-auto").checked = false;
    setAuto(false);
    toast("Auto-share stopped: allow clipboard access to use it.", { tone: "error", icon: "alert" });
  } finally {
    ticking = false;
  }
}

// ------------------------------------------------------------- receiving

async function onReceived(latest) {
  await refresh();
  const item = clip.items.find((i) => i.id === latest);
  if (!item || item.direction !== "received") return;
  const from = item.device.name;
  if (item.kind === "text") clip.lastText = item.text;
  if (document.hasFocus() && (await copyItem(item, { announce: false }))) {
    toast(`Clipboard from ${from} — copied`, { tone: "success", icon: "copy" });
  } else {
    toast(`Clipboard from ${from}`, { icon: "copy", duration: 8000, action: { label: "Copy", run: () => copyItem(item) } });
  }
  notify(`Clipboard from ${from}`, { body: item.kind === "image" ? "An image" : item.transient ? "Something private" : item.text.slice(0, 120), tag: "clipboard" });
  window.dispatchEvent(new CustomEvent("ot-clipboard-received", { detail: item }));
}

function onState(event) {
  const data = event.detail;
  if (data.me.kind !== "owner") return;
  const paired = data.devices.filter((d) => d.paired && d.kind === "app").length;
  const latest = data.clipboard?.latest || "";
  const sharing = data.clipboard?.sharing ?? 0;
  if (clip.latest === null) {
    clip.latest = latest; // what was there before this page opened isn't news
    clip.paired = paired;
    clip.sharing = sharing;
    refresh();
    return;
  }
  if (latest && latest !== clip.latest) {
    clip.latest = latest;
    onReceived(latest);
  } else if (paired !== clip.paired || sharing !== clip.sharing) {
    refresh();
  }
  clip.paired = paired;
  clip.sharing = sharing;
}

// -------------------------------------------------------------- settings

function renderSettings() {
  const box = $("#clip-devices");
  if (!clip.devices.length) {
    box.replaceChildren(h("p", { class: "muted" }, "No paired devices yet. Pair one with Add device → Enter a code."));
    return;
  }
  box.replaceChildren(
    ...clip.devices.map((d) =>
      h(
        "label",
        { class: "clip-device" },
        h("input", { type: "checkbox", role: "switch", checked: d.allowed, onchange: (event) => setAllowed(d.id, event.target.checked) }),
        h("span", { class: "clip-device-name" }, d.name),
        h("span", { class: "muted" }, d.online ? "Nearby" : "Away"),
      ),
    ),
  );
  $("#clip-max-items").value = String(clip.settings?.max_items ?? 50);
  $("#clip-max-age").value = String(clip.settings?.max_age_days ?? 7);
}

async function saveSettings(change) {
  try {
    const { data } = await api("/api/clipboard/settings", { method: "PUT", json: change });
    clip.settings = data.settings;
    clip.devices = data.devices;
  } catch (err) {
    toast(err.message, { tone: "error", icon: "alert" });
  }
  refresh();
}

function setAllowed(id, on) {
  const allowed = new Set(clip.devices.filter((d) => d.allowed).map((d) => d.id));
  if (on) allowed.add(id);
  else allowed.delete(id);
  saveSettings({ allowed: [...allowed] });
}

export function initClipboard() {
  const section = $("#clipboard-section");
  if (!section) return;
  window.addEventListener("ot-mesh-state", onState);
  $("#clip-push").addEventListener("click", pushClipboard);
  $("#clip-search").addEventListener("input", () => refresh());
  $("#clip-clear").addEventListener("click", async () => {
    const ok = await confirmDialog({ title: "Clear clipboard history?", text: "This only removes it from this device.", confirm: "Clear" });
    if (!ok) return;
    await api("/api/clipboard", { method: "DELETE" }).catch(() => {});
    refresh();
  });
  $("#clip-settings").addEventListener("click", () => {
    renderSettings();
    $("#clip-dialog").showModal();
  });
  $("#clip-max-items").addEventListener("change", (event) => saveSettings({ max_items: Number(event.target.value) }));
  $("#clip-max-age").addEventListener("change", (event) => saveSettings({ max_age_days: Number(event.target.value) }));
  const auto = $("#clip-auto");
  auto.hidden = !(navigator.clipboard?.readText && window.isSecureContext);
  auto.closest("label").hidden = auto.hidden;
  auto.checked = autoEnabled();
  auto.addEventListener("change", () => setAuto(auto.checked));
  if (auto.checked) setAuto(true);
}
