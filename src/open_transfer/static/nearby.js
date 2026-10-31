// Open Transfer — nearby devices: the device grid, choosing recipients,
// sending, incoming "Accept?" prompts, pairing (show / enter / scan a code)
// and renaming this device.

import { DIRECT_MAX_BYTES, directSupported, handleSignals, sendDirect } from "./direct.js";
import { $, ApiError, addHistory, api, chunkLimits, copyText, formatBytes, formatDuration, guessKind, h, icon, looksSensitive, newUploadId, plural, pushSample, SENSITIVE_REASONS, shouldChunk, sparklineSVG, state, toast, uploadParallel } from "./lib.js";
import { saveThumb } from "./activity.js";
import { isImage, makeThumb } from "./media.js";
import { notify } from "./notify.js";
import { collectDropped, zipFiles, zipName } from "./zip.js";

const PLATFORM = { windows: "Windows", macos: "Mac", linux: "Linux", android: "Android", ios: "iOS", chromeos: "ChromeOS", unknown: "" };
const ACTIVE = new Set(["offering", "waiting", "accepted", "sending"]);
const FINAL = new Set(["done", "declined", "expired", "canceled", "failed"]);

export const mesh = {
  data: null,
  etag: null,
  selected: new Set(),
  staged: [], // File objects waiting for recipients
  jobs: new Map(), // id -> local job (owns the File objects)
  prompted: new Set(),
  promptQueue: [],
  promptId: null,
  seen: new Map(), // incoming id -> last state
  paired: null, // ids of paired devices (to notice new pairings)
  pairTimer: null, // keeps Add device's pairing window open while the sheet shows
  directFiles: [], // files received straight from another browser (kept in memory)
  tiles: new Map(), // device id -> { el, sig }
  incomingRows: new Map(),
  favorites: loadFavorites(), // [{ id, name, form, platform, kind }], first = Alt+1
  quickTarget: null, // device id the quick-send file picker is for
  stagedUrls: new Map(), // staged image File -> object URL for its thumbnail
  queue: [], // sends waiting their turn: { qid, ids, files }
  starting: 0, // sends being created right now (they count against the limit)
};

// Two sends at a time; more wait in line (and can be reordered or removed).
const MAX_ACTIVE_SENDS = 2;
let queueSeq = 0;

let ctx = { requestPoll: () => {}, droppedFiles: () => [], libraryChanged: () => {} };

// ------------------------------------------------------------- identity

const nativeApp = () => window.OpenTransferAndroid;

function guessDevice() {
  const ua = navigator.userAgent;
  const touchMac = /Macintosh/.test(ua) && navigator.maxTouchPoints > 1;
  if (/iPad/.test(ua) || touchMac) return { name: "iPad", form: "tablet", platform: "ios" };
  if (/iPhone|iPod/.test(ua)) return { name: "iPhone", form: "phone", platform: "ios" };
  if (/Android/.test(ua)) {
    const phone = /Mobile/.test(ua);
    const samsung = /SamsungBrowser|SM-[A-Z]\d/.test(ua);
    const name = samsung ? (phone ? "Galaxy phone" : "Galaxy Tab") : phone ? "Android phone" : "Android tablet";
    return { name, form: phone ? "phone" : "tablet", platform: "android" };
  }
  if (/CrOS/.test(ua)) return { name: "Chromebook", form: "computer", platform: "chromeos" };
  if (/Windows/.test(ua)) return { name: "Windows PC", form: "computer", platform: "windows" };
  if (/Macintosh/.test(ua)) return { name: "Mac", form: "computer", platform: "macos" };
  if (/Linux/.test(ua)) return { name: "Linux PC", form: "computer", platform: "linux" };
  return { name: "Browser", form: "computer", platform: "unknown" };
}

function storedIdentity() {
  try {
    const saved = JSON.parse(localStorage.getItem("ot-me") || "null");
    if (saved && typeof saved.name === "string") return saved;
  } catch {
    /* storage blocked: fall back to a guess */
  }
  return null;
}

function rememberIdentity(identity) {
  try {
    localStorage.setItem("ot-me", JSON.stringify(identity));
  } catch {
    /* not essential */
  }
}

export async function announceSelf() {
  if (state.info.owner) return;
  const guess = guessDevice();
  const saved = storedIdentity();
  try {
    await api("/api/me", { method: "POST", json: { ...guess, ...(saved || {}) } });
  } catch {
    /* the next poll will try again via the session */
  }
}

// ----------------------------------------------------------------- state

export async function fetchState() {
  const headers = mesh.etag ? { "If-None-Match": `"${mesh.etag}"` } : {};
  const result = await api("/api/state", { headers });
  if (!result.notModified) {
    mesh.etag = (result.res.headers.get("ETag") || "").replace(/^W\//, "").replace(/"/g, "") || null;
    mesh.data = result.data;
    apply();
  } else {
    pumpJobs();
  }
  return mesh.data;
}

const jobActive = (job) => job.state === "waiting" || job.state === "running";

export function busy() {
  const receiving = (mesh.data?.incoming || []).some((s) => s.state === "accepted" || s.state === "receiving");
  return [...mesh.jobs.values()].some((j) => jobActive(j) && !j.paused) || mesh.queue.length > 0 || mesh.promptQueue.length > 0 || receiving;
}

function apply() {
  const data = mesh.data;
  const online = new Set(data.devices.filter((d) => d.online).map((d) => d.id));
  for (const id of mesh.selected) if (!online.has(id)) mesh.selected.delete(id);
  const paired = new Set(data.devices.filter((d) => d.paired).map((d) => d.id));
  if (mesh.paired) {
    for (const d of data.devices) {
      if (d.paired && !mesh.paired.has(d.id) && d.kind === "app") {
        toast(`Paired with ${d.name}`, { tone: "success", icon: "link" });
        if ($("#connect-dialog").open) $("#connect-dialog").close();
      }
    }
  }
  mesh.paired = paired;
  if ($("#connect-dialog").open && data.pairing) fillPairing();
  renderMe();
  renderDevices();
  handleIncoming();
  pumpJobs();
  renderJobs();
  renderSendBar();
  if (!state.info.owner) ctx.libraryChanged(inboxFiles());
  if (data.signals) fetchSignals();
  window.dispatchEvent(new CustomEvent("ot-mesh-state", { detail: data }));
}

async function fetchSignals() {
  try {
    const { data } = await api("/api/signals");
    handleSignals(data.signals, directReceiver);
  } catch {
    /* next poll */
  }
}

// Files received over a direct connection live in this page until saved.
const directReceiver = {
  session: (id) => mesh.data?.incoming.find((s) => s.id === id),
  file(file) {
    if (file.size > DIRECT_MAX_BYTES) return;
    const taken = new Set([...mesh.directFiles.map((f) => f.name), ...(mesh.data?.inbox || []).map((f) => f.name)]);
    let name = file.name;
    for (let n = 1; taken.has(name); n += 1) {
      const dot = file.name.lastIndexOf(".");
      name = dot > 0 ? `${file.name.slice(0, dot)} (${n})${file.name.slice(dot)}` : `${file.name} (${n})`;
    }
    mesh.directFiles.unshift({
      name,
      size: file.size,
      mime: file.mime,
      kind: guessKind(name),
      modified: Date.now() / 1000,
      url: URL.createObjectURL(file.blob),
      direct: true,
      saved: false,
    });
    // Cap in-memory files so a peer can't OOM this page.
    while (mesh.directFiles.length > 50) {
      const old = mesh.directFiles.pop();
      try {
        URL.revokeObjectURL(old.url);
      } catch {
        /* already revoked */
      }
    }
    if (!state.info.owner) ctx.libraryChanged(inboxFiles());
  },
};

function inboxFiles() {
  return [...mesh.directFiles, ...(mesh.data?.inbox || [])];
}

export function forgetDirectFile(name) {
  const index = mesh.directFiles.findIndex((f) => f.name === name);
  if (index < 0) return false;
  URL.revokeObjectURL(mesh.directFiles[index].url);
  mesh.directFiles.splice(index, 1);
  ctx.libraryChanged(inboxFiles());
  return true;
}

// -------------------------------------------------------------- this device

function deviceIcon(d) {
  if (d.kind === "browser" && d.form === "computer") return "globe";
  if (d.form === "phone") return "phone";
  if (d.form === "tablet") return "tablet";
  return d.platform === "macos" || d.platform === "chromeos" ? "laptop" : "desktop";
}

function describe(d) {
  const platform = PLATFORM[d.platform] || "";
  if (d.kind === "browser") {
    const host = mesh.data?.host;
    const via = d.via_name && d.via_name !== host?.name ? ` · via ${d.via_name}` : "";
    return `${platform ? `${platform} browser` : "Browser"}${via}`;
  }
  return d.host ? `${platform || "App"} · connected` : platform || "Open Transfer";
}

function renderMe() {
  const { me, discovery, host } = mesh.data;
  $("#me-name-text").textContent = me.name;
  $("#me-icon").replaceChildren(icon(deviceIcon({ ...me, kind: me.kind === "owner" ? "app" : "browser" })));
  const status = $("#me-status");
  $("#me-allow").hidden = true;
  const network = mesh.data.network || {};
  const cantReach = network.unreachable_by || [];
  // Only a guess: a device that does reach us proves this PC's firewall lets it in.
  const blocked = network.reachable_by ? [] : network.public || [];
  if (me.kind === "owner" && cantReach.length) {
    // Told by the devices themselves (they hear us, but can't connect).
    const kind = network.public?.length ? "Public" : "Private";
    const blocks = network.blocking_rules || [];
    // Block beats Allow: name the rule (left by a cancelled Windows prompt) and say the button removes it.
    const fix = blocks.length
      ? ` A Windows Firewall rule (“${blocks[0]}”) blocks Open Transfer.`
      : me.platform === "windows"
        ? ` In Windows Security → Firewall, allow Open Transfer on ${kind} networks.`
        : "";
    status.textContent = `${cantReach.join(", ")} can’t reach this device — usually its firewall.${fix}`;
    status.dataset.tone = "warn";
    $("#me-allow").hidden = !network.can_allow;
    $("#me-allow").textContent = blocks.length ? "Remove the block and allow" : "Allow on this network";
  } else if (me.kind === "owner" && blocked.length) {
    // Windows Firewall keeps everyone out on a "Public" network, whatever the app allows.
    status.textContent = `Other devices may not reach this PC: Windows treats “${blocked[0]}” as a Public network. Set it to Private, and allow Open Transfer on Private networks.`;
    status.dataset.tone = "warn";
  } else if (me.kind === "owner") {
    status.textContent = discovery ? "Visible to nearby devices" : "Not discoverable — add devices by code or address";
    status.dataset.tone = discovery ? "ok" : "warn";
  } else {
    status.textContent = me.paired ? `Paired with ${host.name}` : `Connected through ${host.name}`;
    status.dataset.tone = "ok";
  }
}

// -------------------------------------------------------------- favorites

function loadFavorites() {
  try {
    const saved = JSON.parse(localStorage.getItem("ot-favorites") || "[]");
    return Array.isArray(saved) ? saved.filter((f) => f && typeof f.id === "string" && typeof f.name === "string") : [];
  } catch {
    return []; // storage blocked: no favorites
  }
}

function saveFavorites() {
  try {
    localStorage.setItem("ot-favorites", JSON.stringify(mesh.favorites));
  } catch {
    /* storage blocked: favorites last until the page closes */
  }
}

const isFavorite = (id) => mesh.favorites.some((f) => f.id === id);

function toggleFavorite(d) {
  if (isFavorite(d.id)) mesh.favorites = mesh.favorites.filter((f) => f.id !== d.id);
  else mesh.favorites.push({ id: d.id, name: d.name, form: d.form, platform: d.platform, kind: d.kind });
  saveFavorites();
  renderDevices();
}

/** Nearby devices plus favorites that aren't around: favorites first, online before offline. */
function gridDevices() {
  const live = mesh.data.devices;
  let renamed = false;
  for (const d of live) {
    const fav = mesh.favorites.find((f) => f.id === d.id);
    if (fav && fav.name !== d.name) {
      Object.assign(fav, { name: d.name, form: d.form, platform: d.platform });
      renamed = true;
    }
  }
  if (renamed) saveFavorites();
  const known = new Set(live.map((d) => d.id));
  const away = mesh.favorites.filter((f) => !known.has(f.id)).map((f) => ({ kind: "app", ...f, online: false }));
  const rank = (d) => (isFavorite(d.id) ? 0 : 2) + (d.online ? 0 : 1);
  return [...live, ...away]
    .map((d, i) => ({ d, i }))
    .sort((a, b) => rank(a.d) - rank(b.d) || a.i - b.i)
    .map(({ d }) => d);
}

/** Online favorites in grid order (Alt+1 is the first). */
function onlineFavorites() {
  const online = new Map(onlineDevices().map((d) => [d.id, d]));
  return gridDevices().filter((d) => isFavorite(d.id) && online.has(d.id));
}

function quickSend(id) {
  mesh.quickTarget = id;
  $("#quick-input").click();
}

// -------------------------------------------------------------- device grid

function activityFor(deviceId) {
  // The newest of our transfers that involves this device.
  for (const job of [...mesh.jobs.values()].reverse()) {
    const view = serverJob(job.id);
    const target = view?.targets.find((t) => t.id === deviceId);
    if (target) return { job, view, target };
  }
  return null;
}

function tileStatus(d) {
  if (!d.online) return { text: "Not nearby", tone: "muted" };
  const activity = activityFor(d.id);
  if (activity) {
    const { view, job } = activity;
    const target = effectiveTarget(job, activity.target);
    const pct = view.total ? Math.min(100, Math.round((target.sent / view.total) * 100)) : 0;
    switch (target.state) {
      case "offering":
      case "waiting":
        return { text: "Waiting…", tone: "accent", ring: "spin" };
      case "accepted":
        return job.state === "running" ? { text: "Sending…", tone: "accent", ring: 0 } : { text: "Accepted", tone: "accent", ring: 0 };
      case "sending":
        return { text: `Sending ${pct}%`, tone: "accent", ring: pct };
      case "done":
        if (Date.now() - (job.finishedAt || Date.now()) < 8000) return { text: "Sent", tone: "success", ring: 100 };
        break;
      case "declined":
        if (Date.now() - (job.finishedAt || Date.now()) < 8000) return { text: "Declined", tone: "danger" };
        break;
      case "failed":
      case "expired":
        if (Date.now() - (job.finishedAt || Date.now()) < 8000) return { text: target.state === "expired" ? "No answer" : "Failed", tone: "danger" };
        break;
      default:
        break;
    }
  }
  // Heard on the Wi-Fi but not answering: almost always its firewall.
  if (d.reachable === false) return { text: "Can’t connect", tone: "danger" };
  return { text: describe(d), tone: "muted" };
}

function buildTile(d) {
  const ring = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  ring.setAttribute("class", "device-ring");
  ring.setAttribute("viewBox", "0 0 64 64");
  ring.setAttribute("aria-hidden", "true");
  ring.innerHTML = '<circle class="ring-track" cx="32" cy="32" r="30"/><circle class="ring-value" cx="32" cy="32" r="30" pathLength="100"/>';
  const el = h(
    "li",
    { class: "device", dataset: { id: d.id } },
    h(
      "button",
      { class: "device-button", type: "button", "aria-pressed": "false" },
      h("span", { class: "device-avatar" }, icon(deviceIcon(d)), ring, h("span", { class: "device-check", "aria-hidden": "true" }, icon("check"))),
      h("span", { class: "device-name" }),
      h("span", { class: "device-sub" }),
    ),
    h("span", { class: "device-badge", title: "Paired", "aria-label": "Paired" }, icon("link")),
    h("span", { class: "device-drop-hint", "aria-hidden": "true" }),
    h("span", { class: "device-signal", role: "img", hidden: true }),
    h("button", { class: "icon-btn device-fav", type: "button", "aria-pressed": "false", onclick: () => toggleFavorite(el._device) }, icon("star")),
    h("button", { class: "icon-btn device-quick", type: "button", hidden: true, onclick: () => quickSend(el.dataset.id) }, icon("send")),
  );
  const button = el.querySelector(".device-button");
  button.addEventListener("click", () => toggle(el.dataset.id));
  // Drop files straight onto a device to send them to it — the AirDrop gesture.
  // Onto a selected device: to every selected device.
  el.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.stopPropagation();
    if (!el.dataset.online) {
      event.dataTransfer.dropEffect = "none";
      return;
    }
    event.dataTransfer.dropEffect = "copy";
    el.classList.add("is-drop-target");
    const ids = dropRecipients(el.dataset.id);
    announceDrop(ids.length > 1 ? `Release to send to ${ids.length} selected devices` : `Release to send to ${deviceName(el.dataset.id)}`);
  });
  el.addEventListener("dragleave", () => el.classList.remove("is-drop-target"));
  el.addEventListener("drop", (event) => {
    el.classList.remove("is-drop-target");
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.stopPropagation();
    document.dispatchEvent(new CustomEvent("ot-drop-handled"));
    if (!el.dataset.online) {
      toast(`${el._device.name} isn’t nearby right now — nothing was sent`, { tone: "error", icon: "alert" });
      return;
    }
    sendDropped(event, dropRecipients(el.dataset.id));
  });
  return el;
}

const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");

/** Who a drop onto ``id`` goes to: every selected device if ``id`` is one of them. */
function dropRecipients(id) {
  const online = new Set(onlineDevices().map((d) => d.id));
  const selected = [...mesh.selected].filter((s) => online.has(s));
  return selected.length > 1 && selected.includes(id) ? selected : [id];
}

function announceDrop(text) {
  const live = $("#drop-live");
  if (live && live.textContent !== text) live.textContent = text;
}

/** Send what was dropped; folders are zipped on the fly and sent as one file. */
async function sendDropped(event, ids) {
  let files = [];
  try {
    const dropped = await collectDropped(event);
    if (dropped.folders) {
      toast(`Zipping ${plural(dropped.files.length, "file")} from ${plural(dropped.folders, "folder")}…`, { icon: "folder" });
      const blob = await zipFiles(dropped.files);
      files = [new File([blob], zipName(dropped.files), { type: "application/zip" })];
    } else {
      files = dropped.files.map((entry) => entry.file || entry);
    }
  } catch {
    files = ctx.droppedFiles(event);
  }
  if (!files.length || !(await confirmRecipients(ids, files))) return;
  await startSend(ids, files);
}

function updateTile(el, d) {
  el._device = d;
  const selected = mesh.selected.has(d.id);
  const favorite = isFavorite(d.id);
  const status = tileStatus(d);
  el.dataset.online = d.online ? "1" : "";
  el.dataset.kind = d.kind;
  el.classList.toggle("is-selected", selected);
  el.classList.toggle("is-offline", !d.online);
  el.classList.toggle("is-paired", Boolean(d.paired));
  el.classList.toggle("is-favorite", favorite);
  el.dataset.tone = status.tone;
  const fav = el.querySelector(".device-fav");
  fav.setAttribute("aria-pressed", String(favorite));
  fav.setAttribute("aria-label", favorite ? `Remove ${d.name} from favorites` : `Add ${d.name} to favorites`);
  fav.title = favorite ? "Favorite" : "Add to favorites";
  const quick = el.querySelector(".device-quick");
  quick.hidden = !(favorite && d.online);
  quick.setAttribute("aria-label", `Quick send to ${d.name}`);
  quick.title = `Send files to ${d.name}`;
  const together = selected ? [...mesh.selected].filter((id) => onlineDevices().some((o) => o.id === id)).length : 0;
  const hint = together > 1 ? `Drop to send to ${together}` : "Drop to send";
  const hintEl = el.querySelector(".device-drop-hint");
  if (hintEl.textContent !== hint) hintEl.textContent = hint;
  const button = el.querySelector(".device-button");
  button.disabled = !d.online;
  button.setAttribute("aria-pressed", String(selected));
  button.setAttribute("aria-label", `${d.name}, ${status.text}${d.paired ? ", paired" : ""}${selected ? ", selected" : ""}`);
  const signal = el.querySelector(".device-signal");
  const rtt = d.kind === "app" && d.online && Number.isFinite(d.rtt) ? d.rtt : null;
  const level = rtt === null ? null : rtt < 40 ? "good" : rtt < 150 ? "fair" : "slow";
  signal.hidden = level !== "slow"; // a normal connection (a phone on Wi-Fi is often "fair") needs no warning
  if (rtt !== null) {
    signal.dataset.level = level;
    signal.setAttribute("aria-label", `Connection: ${level} (${rtt} ms)`);
    signal.title = `Connection: ${level} · ${rtt} ms round trip`;
  }
  button.title = d.reachable === false
    ? `${d.name} is on this Wi-Fi but not answering — usually its firewall. On Windows: set the network to Private and allow Open Transfer on Private networks.`
    : [d.name, d.address, rtt !== null && `${rtt} ms`].filter(Boolean).join(" · ");
  const name = el.querySelector(".device-name");
  if (name.textContent !== d.name) name.textContent = d.name;
  const sub = el.querySelector(".device-sub");
  if (sub.textContent !== status.text) sub.textContent = status.text;
  sub.dataset.tone = status.tone;
  const ring = el.querySelector(".device-ring");
  ring.dataset.mode = status.ring === "spin" ? "spin" : status.ring == null ? "off" : "value";
  if (typeof status.ring === "number") ring.style.setProperty("--pct", String(status.ring));
}

function renderDevices() {
  const grid = $("#device-grid");
  const devices = gridDevices();
  let cursor = grid.firstElementChild;
  const ids = new Set();
  for (const d of devices) {
    ids.add(d.id);
    let entry = mesh.tiles.get(d.id);
    if (!entry) {
      entry = { el: buildTile(d) };
      entry.el.classList.add("is-entering");
      mesh.tiles.set(d.id, entry);
    }
    updateTile(entry.el, d);
    if (entry.el !== cursor) grid.insertBefore(entry.el, cursor);
    else cursor = cursor.nextElementSibling;
  }
  for (const [id, entry] of mesh.tiles) {
    if (!ids.has(id)) {
      entry.el.remove();
      mesh.tiles.delete(id);
    }
  }
  const online = devices.filter((d) => d.online);
  const everyone = $("#everyone-target");
  everyone.dataset.ready = online.length >= 2 ? "1" : "";
  $("#everyone-count").textContent = plural(online.length, "device");
  $("#nearby-empty").hidden = devices.length > 0;
  grid.hidden = devices.length === 0;
  $("#nearby-meta").textContent = online.length ? plural(online.length, "device") : "";
  const all = $("#select-all");
  all.hidden = online.length < 2;
  const allSelected = online.length > 0 && online.every((d) => mesh.selected.has(d.id));
  all.textContent = allSelected ? "Deselect all" : "Select all";
  const owner = mesh.data.me.kind === "owner";
  $("#nearby-empty-text").textContent = owner
    ? mesh.data.discovery
      ? "Open Open Transfer on your other devices — they’ll appear here. Phones without the app can scan this device’s QR code."
      : "Discovery is off. Add a device with its code or address."
    : `Nobody else is here yet. Open Open Transfer on another device, or scan ${mesh.data.host.name}’s QR code with it.`;
}

function toggle(id) {
  if (mesh.selected.has(id)) mesh.selected.delete(id);
  else mesh.selected.add(id);
  renderDevices();
  renderSendBar();
}

function onlineDevices() {
  return (mesh.data?.devices || []).filter((d) => d.online);
}

function deviceName(id) {
  return mesh.data?.devices.find((d) => d.id === id)?.name || "device";
}

// -------------------------------------------------------------- send bar

export function stageFiles(files) {
  if (!files.length) return;
  mesh.staged.push(...files);
  if (mesh.selected.size) {
    renderSendBar();
    return;
  }
  renderSendBar();
  const count = onlineDevices().length;
  toast(count ? `${plural(mesh.staged.length, "file")} ready — choose who to send to` : "Files ready — waiting for a device to appear", { icon: "send" });
}

export function stageText(text, filename = "") {
  const clean = String(text || "").replace(/\r\n/g, "\n");
  if (!clean.trim()) return null;
  if (clean.length > 200000) throw new Error("That text is too long (200,000 characters max).");
  let name = String(filename || "").trim() || `note-${new Date().toISOString().slice(0, 16).replace("T", "-").replace(":", "")}.txt`;
  if (!name.toLowerCase().endsWith(".txt")) name += ".txt";
  const file = new File([clean], name, { type: "text/plain" });
  stageFiles([file]);
  return file;
}

const TEXT_TEMPLATES = {
  link: { text: "https://", name: "link.txt" },
  wifi: { text: "Wi-Fi: \nPassword: ", name: "wifi.txt" },
  code: { text: "// paste your code here\n", name: "snippet.txt" },
  note: { text: "", name: "note.txt" },
};

function setupTextDialog() {
  const dialog = $("#text-dialog");
  if (!dialog) return;
  const input = $("#text-input");
  const nameInput = $("#text-filename");
  const count = $("#text-count");
  const lines = $("#text-lines");
  const meter = $("#text-meter-fill");
  const clear = $("#text-clear");
  const submit = $("#text-submit");
  const MAX = 200000;
  const update = () => {
    const len = input.value.length;
    const words = input.value.trim() ? input.value.trim().split(/\s+/).length : 0;
    const lineCount = input.value ? input.value.split("\n").length : 1;
    count.textContent = `${len.toLocaleString()} / 200,000`;
    if (lines) lines.textContent = `${lineCount} ${lineCount === 1 ? "line" : "lines"} · ${words} ${words === 1 ? "word" : "words"} · ~${formatBytes(new Blob([input.value]).size)}`;
    if (meter) {
      const pct = Math.min(100, (len / MAX) * 100);
      meter.style.width = `${pct}%`;
      meter.dataset.tone = len > MAX * 0.95 ? "danger" : len > MAX * 0.8 ? "warn" : "";
    }
    if (clear) clear.hidden = !len;
    if (submit) submit.disabled = !len;
  };
  input.addEventListener("input", () => {
    $("#text-error").textContent = "";
    // Auto-grow a little (capped by CSS max-height via rows + resize).
    input.style.height = "auto";
    update();
  });
  for (const chip of dialog.querySelectorAll(".chip")) {
    chip.addEventListener("click", () => {
      const tpl = TEXT_TEMPLATES[chip.dataset.template];
      if (!tpl) return;
      if (!input.value) {
        input.value = tpl.text;
        if (!nameInput.value && tpl.name !== "note.txt") nameInput.value = tpl.name;
      } else if (!input.value.endsWith("\n")) {
        input.value += `\n${tpl.text}`;
      } else {
        input.value += tpl.text;
      }
      $("#text-error").textContent = "";
      update();
      input.focus();
      // Put the caret at the end (or select the placeholder URL).
      input.setSelectionRange(input.value.length, input.value.length);
    });
  }
  clear?.addEventListener("click", () => {
    input.value = "";
    $("#text-error").textContent = "";
    update();
    input.focus();
  });
  nameInput.addEventListener("input", () => {
    $("#text-error").textContent = "";
  });
  $("#send-text")?.addEventListener("click", () => {
    $("#text-error").textContent = "";
    update();
    dialog.showModal();
    requestAnimationFrame(() => input.focus());
  });
  $("#text-cancel")?.addEventListener("click", () => dialog.close());
  $("#text-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    try {
      const file = stageText(input.value, nameInput.value);
      if (!file) {
        $("#text-error").textContent = "Type something first.";
        return;
      }
      input.value = "";
      nameInput.value = "";
      input.style.height = "";
      update();
      dialog.close();
      toast(`Text staged as “${file.name}” — choose who to send to`, { icon: "send" });
    } catch (err) {
      $("#text-error").textContent = err.message;
    }
  });
  update();
}

function renderSendBar() {
  const bar = $("#send-bar");
  const files = mesh.staged;
  const ids = [...mesh.selected];
  bar.hidden = !files.length && !ids.length;
  document.body.classList.toggle("has-send-bar", !bar.hidden);
  if (bar.hidden) return;
  renderStagedThumbs(files);
  const size = files.reduce((sum, f) => sum + f.size, 0);
  $("#send-bar-files").textContent = files.length
    ? files.length === 1
      ? `${files[0].name} · ${formatBytes(size)}`
      : `${plural(files.length, "file")} · ${formatBytes(size)}`
    : "No files chosen yet";
  const names = ids.map(deviceName);
  $("#send-bar-to").textContent = names.length
    ? `To ${names.length > 3 ? `${names.slice(0, 2).join(", ")} and ${names.length - 2} more` : names.join(", ")}`
    : "Tap the devices to send to";
  const go = $("#send-go");
  go.disabled = !files.length || !ids.length;
  go.querySelector("span").textContent = ids.length > 1 ? `Send to ${ids.length}` : "Send";
}

/** Sending to 3+ devices, or to everyone nearby, asks first. */
async function confirmRecipients(ids, files) {
  const online = onlineDevices();
  const everyone = ids.length >= 2 && ids.length === online.length;
  if (ids.length < 3 && !everyone) return true;
  return confirmDialog({
    title: everyone ? `Send to everyone nearby?` : `Send to ${ids.length} devices?`,
    text: `${plural(files.length, "file")} (${formatBytes(files.reduce((s, f) => s + f.size, 0))}) will be offered to:`,
    items: ids.map(deviceName),
    confirm: `Send to ${ids.length}`,
  });
}

/** Up to three thumbnails of the staged images (object URLs, revoked when unstaged). */
function renderStagedThumbs(files) {
  const images = files.filter((f) => isImage(f) && f.size <= 25_000_000).slice(0, 3);
  for (const [file, url] of mesh.stagedUrls) {
    if (!images.includes(file)) {
      URL.revokeObjectURL(url);
      mesh.stagedUrls.delete(file);
    }
  }
  const box = $("#send-bar-thumbs");
  const sig = images.map((f) => `${f.name}:${f.size}`).join("|");
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.replaceChildren(
    ...images.map((file) => {
      if (!mesh.stagedUrls.has(file)) mesh.stagedUrls.set(file, URL.createObjectURL(file));
      return h("img", { src: mesh.stagedUrls.get(file), alt: "" });
    }),
  );
}

async function sendStaged() {
  const ids = [...mesh.selected];
  const files = [...mesh.staged];
  if (!ids.length || !files.length) return;
  if (!(await confirmRecipients(ids, files))) return;
  await startSend(ids, files);
}

function activeSends() {
  return [...mesh.jobs.values()].filter(jobActive).length + mesh.starting;
}

/** Start the next queued sends while there's room. */
function pumpQueue() {
  while (mesh.queue.length && activeSends() < MAX_ACTIVE_SENDS) {
    const next = mesh.queue.shift();
    startSend(next.ids, next.files, { queued: true });
  }
  renderJobs();
}

function moveQueued(qid, delta) {
  const i = mesh.queue.findIndex((q) => q.qid === qid);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= mesh.queue.length) return;
  [mesh.queue[i], mesh.queue[j]] = [mesh.queue[j], mesh.queue[i]];
  renderJobs();
  // Keep keyboard focus on the same control after the reorder.
  $(`#job-list [data-job="q:${qid}"] .queue-${delta < 0 ? "up" : "down"}`)?.focus();
}

function removeQueued(qid) {
  mesh.queue = mesh.queue.filter((q) => q.qid !== qid);
  renderJobs();
}

async function startSend(ids, files, { queued = false } = {}) {
  if (!queued && (mesh.queue.length || activeSends() >= MAX_ACTIVE_SENDS)) {
    mesh.queue.push({ qid: ++queueSeq, ids, files });
    mesh.staged = [];
    mesh.selected.clear();
    renderDevices();
    renderSendBar();
    renderJobs();
    toast(`Queued — it starts when one of the ${MAX_ACTIVE_SENDS} sends in progress is done`, { icon: "clock" });
    return;
  }
  mesh.starting += 1;
  let data;
  try {
    ({ data } = await api("/api/send", {
      method: "POST",
      json: { to: ids, files: files.map((f) => ({ name: f.name || "Untitled", size: f.size, mime: f.type || "application/octet-stream" })) },
    }));
  } catch (err) {
    toast(err.message, { tone: "error", icon: "alert" });
    pumpQueue();
    return;
  } finally {
    mesh.starting -= 1;
  }
  const job = { id: data.job.id, files, targets: ids, state: "waiting", index: 0, uploaded: 0, speed: 0, xhr: null, finishedAt: 0 };
  mesh.jobs.set(job.id, job);
  const picture = files.find((f) => isImage(f) && f.size <= 25_000_000);
  if (picture) {
    makeThumb(picture).then((thumb) => {
      job.thumb = thumb;
      scheduleJobRender();
    });
  }
  mesh.staged = [];
  mesh.selected.clear();
  mesh.data.outgoing.push(data.job);
  renderDevices();
  renderSendBar();
  renderJobs();
  ctx.requestPoll();
}

// ---------------------------------------------------------- outgoing jobs

function serverJob(id) {
  return mesh.data?.outgoing.find((j) => j.id === id) || null;
}

function pumpJobs() {
  for (const job of mesh.jobs.values()) {
    if (job.state === "running") {
      checkFinished(job);
      continue;
    }
    if (job.state !== "waiting") continue;
    const view = serverJob(job.id);
    if (!view) {
      if (mesh.data) finishJob(job, "failed");
      continue;
    }
    const states = view.targets.map((t) => t.state);
    if (states.some((s) => s === "offering" || s === "waiting")) continue;
    if (states.some((s) => s === "accepted")) startTransfer(job, view);
    else finishJob(job, "nobody");
  }
}

// A transfer goes straight to browsers that accepted it (WebRTC) and through
// the apps to everyone else; a direct attempt that fails falls back to the app.
function directEligible(job, target) {
  const total = job.files.reduce((n, f) => n + f.size, 0);
  return target.kind === "browser" && directSupported() && total <= DIRECT_MAX_BYTES && !window.OT_NO_DIRECT;
}

/** What to show for a target: our direct attempt overrides the app's view. */
function effectiveTarget(job, target) {
  const d = job.direct?.get(target.id);
  if (!d || d.state === "relay") return target;
  const state = d.state === "done" ? "done" : d.state === "sending" ? "sending" : "accepted";
  return { ...target, state, sent: d.sent, direct: d.state };
}

function setDirect(job, target, value, sent = 0) {
  return api(`/api/send/${encodeURIComponent(job.id)}/targets/${encodeURIComponent(target)}/direct`, {
    method: "POST",
    json: { state: value, sent },
  });
}

async function startTransfer(job, view) {
  job.state = "running";
  job.direct = new Map();
  job.abort = new AbortController();
  const accepted = view.targets.filter((t) => t.state === "accepted");
  await Promise.all(
    accepted
      .filter((t) => directEligible(job, t))
      .map(async (t) => {
        try {
          await setDirect(job, t.id, "trying");
          job.direct.set(t.id, { state: "trying", sent: 0 });
        } catch {
          /* it goes through the app instead */
        }
      }),
  );
  if (accepted.length > job.direct.size) queueRelay(job);
  for (const id of job.direct.keys()) runDirect(job, id);
  renderJobs();
}

async function runDirect(job, target) {
  const d = job.direct.get(target);
  const total = job.files.reduce((n, f) => n + f.size, 0);
  let reported = 0;
  const ok = await sendDirect({
    job: job.id,
    target,
    files: job.files,
    signal: job.abort.signal,
    onProgress: (sent) => {
      d.state = "sending";
      d.sent = sent;
      scheduleJobRender();
      if (Date.now() - reported > 1500) {
        reported = Date.now();
        setDirect(job, target, "progress", sent).catch(() => {});
      }
    },
  });
  if (job.state === "finished") return; // canceled meanwhile
  if (ok) {
    d.state = "done";
    d.sent = total;
    await setDirect(job, target, "done").catch(() => {});
  } else {
    d.state = "relay";
    d.sent = 0;
    await setDirect(job, target, "failed").catch(() => {});
    queueRelay(job);
  }
  mesh.etag = null;
  ctx.requestPoll();
  checkFinished(job);
}

function queueRelay(job) {
  if (job.relayRunning) {
    job.relayQueued = true;
    return;
  }
  Object.assign(job, { relayRunning: true, index: 0, uploaded: 0, fileLoaded: 0 });
  uploadNext(job);
}

function relayDone(job) {
  job.relayRunning = false;
  job.xhr = null;
  if (job.relayQueued) {
    job.relayQueued = false;
    queueRelay(job);
    return;
  }
  checkFinished(job);
}

function checkFinished(job) {
  if (job.state !== "running" || job.relayRunning) return;
  if ([...job.direct.values()].some((d) => d.state === "trying" || d.state === "sending")) return;
  const view = serverJob(job.id);
  const pending = (view?.targets || []).map((t) => effectiveTarget(job, t)).some((t) => ACTIVE.has(t.state));
  if (!pending) finishJob(job, "done");
  else ctx.requestPoll();
}

function finishJob(job, outcome) {
  if (FINAL.has(job.state) || job.state === "finished") return;
  job.state = "finished";
  job.finishedAt = Date.now();
  const view = serverJob(job.id);
  const targets = view?.targets || [];
  const declined = targets.filter((t) => t.state === "declined");
  const failed = targets.filter((t) => ["failed", "expired", "canceled"].includes(t.state));
  const done = targets.filter((t) => t.state === "done");
  if (failed.length || (outcome === "nobody" && declined.length)) {
    const who = (failed[0] || declined[0]).name;
    notify(failed.length ? `Couldn’t send to ${who}` : `${who} declined`, { body: (failed[0] || declined[0]).reason || "", tag: `job-${job.id}` });
  }
  if (outcome === "nobody") {
    if (declined.length === targets.length && targets.length) {
      toast(targets.length === 1 ? `${targets[0].name} declined — you can ask again` : "Everyone declined — you can ask again", {
        tone: "error",
        icon: "x",
        action: { label: "Retry", run: () => retryJob(job) },
        duration: 6000,
      });
    } else if (failed.length) toast(failed[0].reason || `Couldn’t send to ${failed[0].name}`, { tone: "error", icon: "alert" });
  } else if (done.length && !failed.length && !declined.length) {
    notify(done.length === 1 ? `Sent to ${done[0].name}` : `Sent to ${plural(done.length, "device")}`, { tag: `job-${job.id}` });
    toast(done.length === 1 ? `Sent to ${done[0].name}` : `Sent to ${plural(done.length, "device")}`, { tone: "success", icon: "check" });
    const secs = job.startedAt ? Math.max(0.1, (performance.now() - job.startedAt) / 1000) : 0;
    const bytes = job.files.reduce((n, f) => n + f.size, 0);
    const title = job.files.length === 1 ? job.files[0].name : plural(job.files.length, "file");
    // The app records its own window's transfers; a visitor's browser keeps its own list.
    if (state.info.owner) saveThumb(job.id, job.thumb);
    else addHistory({ name: title, size: bytes, direction: "send", status: "done", speed: secs ? bytes / secs : 0, samples: (job.samples || []).slice(-30), thumb: job.thumb || undefined });
    window.dispatchEvent(new CustomEvent("ot-history"));
  } else if (declined.length) {
    toast(declined.length === 1 ? `${declined[0].name} declined — retry to ask again` : `${declined.length} declined — retry to ask again`, {
      tone: "error",
      icon: "x",
      action: { label: "Retry", run: () => retryJob(job) },
      duration: 6000,
    });
  }
  setTimeout(pumpQueue, 0); // a slot is free
  // Bound retained jobs (each holds File blobs): keep retryable ones for the
  // user to dismiss, evict the oldest finished beyond 20.
  const finished = [...mesh.jobs.values()].filter((j) => j.state === "finished");
  if (finished.length > 20) {
    finished.sort((a, b) => a.finishedAt - b.finishedAt);
    for (const old of finished.slice(0, finished.length - 20)) mesh.jobs.delete(old.id);
  }
  renderJobs();
  setTimeout(() => {
    const v = serverJob(job.id);
    if (!v || v.targets.every((t) => t.state === "done")) {
      mesh.jobs.delete(job.id);
      renderJobs();
      renderDevices();
    }
  }, 6000);
}

function uploadNext(job) {
  if (job.state === "finished" || job.paused) return;
  if (job.index >= job.files.length) {
    relayDone(job);
    ctx.requestPoll();
    return;
  }
  const file = job.files[job.index];
  // Big files go chunk-by-chunk so a Wi-Fi drop loses one 2 MiB piece,
  // not the whole file. Resume continues from the server's byte count.
  if (shouldChunk(file.size)) {
    uploadNextChunked(job);
    return;
  }
  const xhr = new XMLHttpRequest();
  job.xhr = xhr;
  job.startedAt = job.startedAt || performance.now();
  job.lastAt = performance.now();
  job.lastLoaded = 0;
  job.fileLoaded = 0;
  xhr.open("PUT", `/api/send/${encodeURIComponent(job.id)}/files/${job.index}`);
  xhr.responseType = "json";
  xhr.timeout = 120000;
  xhr.setRequestHeader("Accept", "application/json");
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) return;
    const now = performance.now();
    const dt = (now - job.lastAt) / 1000;
    if (dt >= 0.3) {
      const instant = (event.loaded - job.lastLoaded) / dt;
      job.speed = job.speed ? job.speed * 0.7 + instant * 0.3 : instant;
      job.lastAt = now;
      job.lastLoaded = event.loaded;
    }
    job.fileLoaded = event.loaded;
    scheduleJobRender();
  });
  xhr.addEventListener("load", () => {
    job.xhr = null;
    if (xhr.status === 200) {
      job.uploaded += file.size;
      job.fileLoaded = 0;
      job.index += 1;
      uploadNext(job);
    } else if (xhr.response?.error?.code === "no_receivers") {
      relayDone(job); // everyone left is being sent to directly
    } else {
      const message = xhr.response?.error?.message || `Sending failed (${xhr.status || "no response"}).`;
      job.error = message;
      relayDone(job);
      if (xhr.status !== 410) toast(message, { tone: "error", icon: "alert" });
    }
    ctx.requestPoll();
  });
  xhr.addEventListener("error", () => {
    job.error = "Connection lost. Check your Wi-Fi and try again.";
    relayDone(job);
    toast(job.error, { tone: "error", icon: "alert" });
  });
  xhr.addEventListener("timeout", () => {
    job.error = "Sending timed out. Check your Wi-Fi and retry.";
    relayDone(job);
    toast(job.error, { tone: "error", icon: "alert" });
  });
  xhr.addEventListener("abort", () => {
    job.xhr = null;
  });
  xhr.send(file);
}

async function uploadNextChunked(job) {
  // Parallel chunks (3 in flight) with pause: aborting keeps the upload id,
  // so resuming reuses the staged bytes instead of starting over.
  const file = job.files[job.index];
  const total = file.size;
  const { size: chunkSize } = chunkLimits();
  const chunks = Math.max(1, Math.ceil(total / chunkSize));
  job.chunkIds = job.chunkIds || {};
  const uploadId = (job.chunkIds[job.index] ||= newUploadId());
  const url = `/api/send/${encodeURIComponent(job.id)}/files/${job.index}`;
  const statusUrl = `${url}/status?upload_id=${encodeURIComponent(uploadId)}`;
  job.abortChunk = new AbortController();
  job.samples = job.samples || [];
  job.startedAt = job.startedAt || performance.now();
  let lastAt = performance.now();
  let lastSent = 0;
  try {
    await uploadParallel(
      url,
      {
        file, total, uploadId, statusUrl, signal: job.abortChunk.signal, concurrency: 3,
        onProgress: (sent) => {
          const now = performance.now();
          const dt = (now - lastAt) / 1000;
          if (dt >= 0.4 && sent > lastSent) {
            const instant = (sent - lastSent) / dt;
            job.speed = job.speed ? job.speed * 0.6 + instant * 0.4 : instant;
            pushSample(job.samples, job.speed);
            lastAt = now;
            lastSent = sent;
          }
          job.fileLoaded = sent;
          job.chunkNote = total > chunkSize ? `${Math.min(chunks, Math.ceil(sent / chunkSize) + 1)}/${chunks} chunks · 3 parallel` : "";
          scheduleJobRender();
        },
      },
    );
    if (job.state === "finished") return;
    job.uploaded += total;
    job.fileLoaded = 0;
    job.chunkNote = "";
    job.index += 1;
    uploadNext(job);
  } catch (err) {
    if (err.paused || job.abortChunk.signal.aborted) {
      if (job.wantCancel || job.state === "finished") return;
      job.paused = true;
      job.chunkNote = `Paused at ${Math.floor((job.fileLoaded / Math.max(1, total)) * 100)}% — resume to continue`;
      scheduleJobRender();
      return;
    }
    if (job.state === "finished") return;
    const message = err.message || "Sending failed. Retry to resume where it stopped.";
    job.error = message;
    job.chunkNote = "";
    relayDone(job);
    if (err.status !== 410) toast(message, { tone: "error", icon: "alert" });
  }
  ctx.requestPoll();
}

function pauseJob(job) {
  if (!jobActive(job) || job.paused) return;
  job.paused = true;
  job.xhr?.abort();
  job.abortChunk?.abort();
  scheduleJobRender();
}

function resumeJob(job) {
  if (!job.paused) return;
  job.paused = false;
  job.chunkNote = "";
  scheduleJobRender();
  if (job.relayRunning) {
    const file = job.files[job.index];
    if (file && shouldChunk(file.size)) uploadNextChunked(job);
    else uploadNext(job);
  } else {
    queueRelay(job);
  }
}

async function cancelJob(job) {
  job.wantCancel = true;
  job.state = "finished";
  job.finishedAt = Date.now();
  job.paused = false;
  job.xhr?.abort();
  job.abort?.abort();
  job.abortChunk?.abort();
  try {
    await api(`/api/send/${encodeURIComponent(job.id)}`, { method: "DELETE" });
  } catch {
    /* already gone */
  }
  pumpQueue();
  ctx.requestPoll();
}

async function skipWaiting(job) {
  const view = serverJob(job.id);
  for (const t of view?.targets || []) {
    if (t.state === "offering" || t.state === "waiting") {
      try {
        await api(`/api/send/${encodeURIComponent(job.id)}/targets/${encodeURIComponent(t.id)}`, { method: "DELETE" });
      } catch {
        /* it answered meanwhile */
      }
    }
  }
  mesh.etag = null;
  ctx.requestPoll();
}

const RETRYABLE = new Set(["declined", "failed", "expired", "canceled"]);

async function retryJob(job, onlyTarget = null) {
  const view = serverJob(job.id);
  const online = new Set(onlineDevices().map((d) => d.id));
  let ids = (view?.targets || []).filter((t) => RETRYABLE.has(t.state) && online.has(t.id)).map((t) => t.id);
  if (onlyTarget) ids = ids.filter((id) => id === onlyTarget);
  if (!ids.length) {
    toast("Those devices aren’t nearby right now.", { tone: "error", icon: "alert" });
    return;
  }
  // Prefer the server-side retry (fresh offers, same file list) so a decline
  // becomes a new "Accept?" prompt on the other side. Fall back to a brand
  // new send when the job already expired server-side (e.g. after reload).
  try {
    const { data } = await api(`/api/send/${encodeURIComponent(job.id)}/retry`, {
      method: "POST",
      json: { to: ids },
    });
    const files = job.files;
    mesh.jobs.delete(job.id);
    const fresh = { id: data.job.id, files, targets: ids, state: "waiting", index: 0, uploaded: 0, speed: 0, xhr: null, finishedAt: 0, retried: true, thumb: job.thumb };
    mesh.jobs.set(fresh.id, fresh);
    mesh.data.outgoing.push(data.job);
    toast(ids.length === 1 ? `Asking ${deviceName(ids[0])} again…` : `Asking ${ids.length} devices again…`, { icon: "retry" });
  } catch {
    mesh.jobs.delete(job.id);
    await startSend(ids, job.files);
    return;
  }
  renderDevices();
  renderSendBar();
  renderJobs();
  ctx.requestPoll();
}

let jobRenderQueued = false;
function scheduleJobRender() {
  if (jobRenderQueued) return;
  jobRenderQueued = true;
  requestAnimationFrame(() => {
    jobRenderQueued = false;
    renderJobs();
  });
}

// How each delivery travels (see mesh._path).
const PATH_HINT = {
  app: "Straight to that device’s app",
  direct: "Browser to browser, straight over your network",
  via: "To a browser, through the app it’s connected to",
  local: "To a browser connected to this device",
};

function pathLabel(path, target) {
  if (path === "via") return `Via ${target.via_name || "its app"}`;
  if (path === "local") return "Via this device";
  return "Direct";
}

const TARGET_TEXT = {
  offering: "Asking…",
  waiting: "Waiting for them to accept…",
  accepted: "Accepted",
  sending: "Receiving…",
  done: "Delivered",
  declined: "Declined",
  expired: "Didn’t answer",
  canceled: "Canceled",
  failed: "Failed",
};

function renderJobs() {
  const list = $("#job-list");
  const keep = new Set();
  for (const job of mesh.jobs.values()) {
    const view = serverJob(job.id);
    if (!view) continue;
    keep.add(job.id);
    let row = list.querySelector(`[data-job="${CSS.escape(job.id)}"]`);
    if (!row) {
      row = h("li", { class: "job is-entering", dataset: { job: job.id } });
      list.append(row);
    }
    const total = view.total || 1;
    const title = job.files.length === 1 ? job.files[0].name : plural(job.files.length, "file");
    const own = Math.min(total, job.uploaded + (job.fileLoaded || 0));
    let headline;
    if (job.paused) headline = job.chunkNote || `Paused · ${formatBytes(own)} of ${formatBytes(view.total)}`;
    else if (job.state === "waiting") headline = `${formatBytes(view.total)} · waiting for ${plural(view.targets.filter((t) => t.state === "waiting" || t.state === "offering").length, "answer")}`;
    else if (job.state === "running" && job.relayRunning) {
      const eta = job.speed > 0 ? formatDuration((total - own) / job.speed) : "";
      headline = [`${formatBytes(own)} of ${formatBytes(view.total)}`, job.speed > 0 && `${formatBytes(job.speed)}/s`, eta, job.chunkNote].filter(Boolean).join(" · ");
    } else if (job.state === "running") headline = `${formatBytes(view.total)} · sending directly`;
    else headline = formatBytes(view.total);
    const anyWaiting = view.targets.some((t) => t.state === "waiting" || t.state === "offering");
    const anyAccepted = view.targets.some((t) => t.state === "accepted");
    const retryable = view.targets.some((t) => RETRYABLE.has(t.state));
    const active = jobActive(job) && !job.paused;
    const pausable = job.state === "running" && job.relayRunning && !job.paused;
    const mainCol = h("div", { class: "row-main" }, h("span", { class: "row-title", title }, title), h("span", { class: "row-sub" }, headline));
    if (job.samples?.length >= 2 && job.state === "running") {
      const spark = sparklineSVG(job.samples);
      if (spark) {
        const wrap = h("span", { class: "spark-wrap", title: "Speed over time" });
        wrap.append(spark);
        mainCol.append(wrap);
      }
    }
    row.replaceChildren(
      h(
        "div",
        { class: "job-head" },
        h("span", { class: "thumb", "aria-hidden": "true" }, job.thumb ? h("img", { class: "is-loaded", src: job.thumb, alt: "" }) : icon("send")),
        mainCol,
        h(
          "div",
          { class: "row-actions is-visible" },
          anyWaiting && anyAccepted && job.state === "waiting" && h("button", { class: "btn btn-tinted btn-small", type: "button", onclick: () => skipWaiting(job) }, "Send now"),
          !active && retryable && h("button", { class: "icon-btn is-accent", type: "button", title: "Retry", "aria-label": `Retry sending ${title}`, onclick: () => retryJob(job) }, icon("retry")),
          job.paused && h("button", { class: "icon-btn is-accent", type: "button", title: "Resume", "aria-label": `Resume sending ${title}`, onclick: () => resumeJob(job) }, icon("retry")),
          pausable && h("button", { class: "icon-btn", type: "button", title: "Pause", "aria-label": `Pause sending ${title}`, onclick: () => pauseJob(job) }, icon("pause")),
          h(
            "button",
            {
              class: "icon-btn",
              type: "button",
              title: active || job.paused ? "Cancel" : "Dismiss",
              "aria-label": `${active || job.paused ? "Cancel" : "Dismiss"} ${title}`,
              onclick: () => (active || job.paused ? cancelJob(job) : (mesh.jobs.delete(job.id), renderJobs(), renderDevices())),
            },
            icon("x"),
          ),
        ),
      ),
      h(
        "ul",
        { class: "job-targets" },
        view.targets.map((original) => {
          const t = effectiveTarget(job, original);
          const pct = t.state === "done" ? 100 : Math.min(100, (t.sent / total) * 100);
          let text = t.state === "sending" ? `${Math.floor(pct)}%` : t.reason && FINAL.has(t.state) && t.state !== "done" ? t.reason : TARGET_TEXT[t.state] || t.state;
          if (t.direct === "trying") text = "Connecting directly…";
          else if (t.direct === "sending") text = `Directly · ${Math.floor(pct)}%`;
          else if (t.direct === "done") text = "Delivered directly";
          const canRetryTarget = RETRYABLE.has(original.state) && onlineDevices().some((d) => d.id === original.id);
          const path = t.direct ? "direct" : original.path || "app";
          return h(
            "li",
            { class: "job-target", dataset: { state: t.state, path } },
            h("span", { class: "mini-avatar", "aria-hidden": "true" }, icon(deviceIcon(t))),
            h("span", { class: "job-target-name" }, t.name),
            h("span", { class: "job-target-path", title: PATH_HINT[path] }, pathLabel(path, original)),
            h("span", { class: "job-target-state" }, text),
            canRetryTarget &&
              h(
                "button",
                {
                  class: "icon-btn is-accent",
                  type: "button",
                  title: original.state === "declined" ? `Ask ${original.name} again` : `Retry ${original.name}`,
                  "aria-label": original.state === "declined" ? `Ask ${original.name} again` : `Retry ${original.name}`,
                  onclick: () => retryJob(job, original.id),
                },
                icon("retry"),
              ),
            h("span", { class: "progress", role: "progressbar", "aria-label": `${t.name}: ${text}`, "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(pct)) }, h("span", { class: "progress-bar", style: { transform: `scaleX(${pct / 100})` } })),
          );
        }),
      ),
    );
  }
  mesh.queue.forEach((q, i) => {
    const key = `q:${q.qid}`;
    keep.add(key);
    let row = list.querySelector(`[data-job="${CSS.escape(key)}"]`);
    if (!row) {
      row = h("li", { class: "job is-queued is-entering", dataset: { job: key } });
    }
    list.append(row); // queued sends always come after the ones in progress, in order
    const title = q.files.length === 1 ? q.files[0].name : plural(q.files.length, "file");
    const to = q.ids.map(deviceName);
    row.replaceChildren(
      h(
        "div",
        { class: "job-head" },
        h("span", { class: "thumb", "aria-hidden": "true" }, icon("clock")),
        h(
          "div",
          { class: "row-main" },
          h("span", { class: "row-title", title }, title),
          h("span", { class: "row-sub" }, `Queued · ${i + 1} of ${mesh.queue.length} · to ${to.length > 2 ? `${to.length} devices` : to.join(" and ")}`),
        ),
        h(
          "div",
          { class: "row-actions is-visible" },
          h("button", { class: "icon-btn queue-up", type: "button", disabled: i === 0, title: "Move up", "aria-label": `Move ${title} up`, onclick: () => moveQueued(q.qid, -1) }, icon("up")),
          h("button", { class: "icon-btn queue-down", type: "button", disabled: i === mesh.queue.length - 1, title: "Move down", "aria-label": `Move ${title} down`, onclick: () => moveQueued(q.qid, 1) }, icon("down")),
          h("button", { class: "icon-btn", type: "button", title: "Remove", "aria-label": `Remove ${title} from the queue`, onclick: () => removeQueued(q.qid) }, icon("x")),
        ),
      ),
    );
  });
  for (const row of [...list.children]) if (!keep.has(row.dataset.job)) row.remove();
  renderIncomingRows();
  const any = list.children.length + $("#incoming-list").children.length;
  $("#activity").hidden = any === 0;
  const sending = [...mesh.jobs.values()].filter(jobActive);
  if (sending.length && !document.title.startsWith("●")) document.title = "Sending · Open Transfer";
  else if (!sending.length && document.title === "Sending · Open Transfer") document.title = "Open Transfer";
}

window.addEventListener("beforeunload", (event) => {
  const unsaved = mesh.directFiles.some((f) => !f.saved);
  if ([...mesh.jobs.values()].some(jobActive) || unsaved) {
    event.preventDefault();
    event.returnValue = "";
  }
});

// ---------------------------------------------------------------- incoming

function handleIncoming() {
  const owner = mesh.data.me.kind === "owner";
  for (const s of mesh.data.incoming) {
    const before = mesh.seen.get(s.id);
    if (s.state === "pending" && !mesh.prompted.has(s.id)) {
      mesh.prompted.add(s.id);
      mesh.promptQueue.push(s.id);
      const count = s.files.length === 1 ? "a file" : `${s.files.length} files`;
      notify(`${s.from.name} wants to send you ${count}`, {
        body: s.files.map((f) => f.name).slice(0, 3).join(", ") + (s.files.length > 3 ? "…" : ""),
        tag: `offer-${s.id}`,
      });
    }
    if (before && before !== s.state) {
      const from = s.from.name;
      if (s.state === "done") {
        const what = s.files.length === 1 ? `“${s.files[0].saved_name || s.files[0].name}”` : `${s.files.length} files`;
        notify(`Received ${what} from ${from}`, { tag: `in-${s.id}` });
        toast(s.files.length === 1 ? `Received “${s.files[0].saved_name || s.files[0].name}” from ${from}` : `Received ${s.files.length} files from ${from}`, {
          tone: "success",
          icon: "check",
          duration: 5000,
          action: !owner && s.files.length === 1 ? { label: "Save", run: () => saveInboxFile(s.files[0].saved_name || s.files[0].name) } : undefined,
        });
        const title = s.files.length === 1 ? s.files[0].saved_name || s.files[0].name : plural(s.files.length, "file");
        const entry = { name: `${title} — from ${from}`, size: s.total, direction: "receive", status: "done", speed: 0 };
        const picture = s.files.find((f) => isImage(f) && f.size <= 25_000_000);
        const name = picture && (picture.saved_name || picture.name);
        const url = name && (owner ? `/files/${encodeURIComponent(name)}?inline=1` : `/api/inbox/files/${encodeURIComponent(name)}?inline=1`);
        if (!owner) {
          (url ? makeThumb(url) : Promise.resolve(null)).then((thumb) => {
            addHistory({ ...entry, thumb: thumb || undefined });
            window.dispatchEvent(new CustomEvent("ot-history"));
          });
        }
        ctx.libraryChanged(null);
      } else if (s.state === "canceled" && before !== "pending") toast(`${from} stopped sending`, { tone: "error", icon: "x" });
      else if (s.state === "failed") {
        notify(`Couldn’t receive files from ${from}`, { body: s.files.find((f) => f.error)?.error || s.reason || "", tag: `in-${s.id}` });
        toast(`Couldn’t receive files from ${from}`, { tone: "error", icon: "alert" });
      }
    }
    mesh.seen.set(s.id, s.state);
  }
  showNextPrompt();
  const waiting = mesh.data.incoming.filter((s) => s.state === "pending");
  if (waiting.length && document.hidden) document.title = `● ${waiting[0].from.name} wants to send you files`;
  else if (document.title.startsWith("●")) document.title = "Open Transfer";
}

function saveInboxFile(name) {
  const a = h("a", { href: `/api/inbox/files/${encodeURIComponent(name)}`, download: name });
  document.body.append(a);
  a.click();
  a.remove();
}

let lastFocus = null;

function showNextPrompt() {
  const dialog = $("#incoming-dialog");
  const current = mesh.promptId && mesh.data.incoming.find((s) => s.id === mesh.promptId);
  if (mesh.promptId && (!current || current.state !== "pending")) {
    if (current && current.state === "expired") toast(`The transfer from ${current.from.name} expired`, { icon: "alert" });
    if (current && current.state === "canceled") toast(`${current.from.name} canceled`, { icon: "x" });
    mesh.promptId = null;
    if (dialog.open) dialog.close();
    if (lastFocus?.isConnected) lastFocus.focus();
    lastFocus = null;
  }
  if (mesh.promptId) {
    updatePromptCountdown(current);
    return;
  }
  while (mesh.promptQueue.length) {
    const id = mesh.promptQueue.shift();
    const s = mesh.data.incoming.find((x) => x.id === id && x.state === "pending");
    if (!s) continue;
    mesh.promptId = id;
    fillPrompt(s);
    if (!dialog.open) {
      lastFocus = document.activeElement;
      dialog.showModal();
      // Move focus into the dialog for keyboard + screen-reader users.
      requestAnimationFrame(() => $("#incoming-accept").focus());
    }
    return;
  }
}

function fillPrompt(s) {
  const from = s.from;
  $("#incoming-avatar").replaceChildren(icon(deviceIcon({ ...from, kind: from.via_name ? "browser" : "app" })));
  $("#incoming-title").textContent = `${from.name} wants to send you ${s.files.length === 1 ? "a file" : `${s.files.length} files`}`;
  const host = mesh.data.host;
  const via = from.via_name && from.via_name !== host.name ? ` · via ${from.via_name}` : "";
  $("#incoming-sub").textContent = `${formatBytes(s.total)}${via}${s.paired ? " · paired" : ""}`;
  const shown = s.files.slice(0, 5);
  $("#incoming-files").replaceChildren(
    ...shown.map((f) =>
      h(
        "li",
        {},
        h("span", { class: "incoming-file-name" }, f.name),
        f.duplicate && h("span", { class: "incoming-file-dupe" }, "Already here"),
        h("span", { class: "incoming-file-size" }, formatBytes(f.size)),
      ),
    ),
    ...(s.files.length > shown.length ? [h("li", { class: "muted" }, `and ${s.files.length - shown.length} more`)] : []),
  );
  // Same name and size as a file already on this device: keep both, skip or replace.
  const dupes = s.files.filter((f) => f.duplicate).length;
  $("#incoming-dupes").hidden = dupes === 0;
  if (dupes) {
    const one = dupes === 1;
    $("#incoming-dupes-text").textContent = `${one ? "1 of these files is" : `${dupes} of these files are`} already here (same name and size).`;
    $("#dupes-skip").textContent = one ? "Skip it" : "Skip them";
    $("#dupes-replace").textContent = one ? "Replace it" : "Replace them";
    for (const radio of document.querySelectorAll("input[name=dupes]")) radio.checked = radio.value === "rename";
  }
  $("#incoming-accept").disabled = false;
  $("#incoming-decline").disabled = false;
  updatePromptCountdown(s);
}

function updatePromptCountdown(s) {
  if (!s || s.expires_in == null) return;
  const m = Math.floor(s.expires_in / 60);
  const sec = String(s.expires_in % 60).padStart(2, "0");
  $("#incoming-expiry").textContent = `Expires in ${m}:${sec}`;
}

async function decide(accept) {
  const id = mesh.promptId;
  if (!id) return;
  $("#incoming-accept").disabled = true;
  $("#incoming-decline").disabled = true;
  const duplicates = $("#incoming-dupes").hidden ? "rename" : document.querySelector("input[name=dupes]:checked")?.value || "rename";
  try {
    await api(`/api/incoming/${encodeURIComponent(id)}/${accept ? "accept" : "decline"}`, { method: "POST", json: accept ? { duplicates } : {} });
  } catch (err) {
    toast(err.message, { tone: "error", icon: "alert" });
  }
  mesh.promptId = null;
  $("#incoming-dialog").close();
  mesh.etag = null;
  ctx.requestPoll();
}

function renderIncomingRows() {
  const list = $("#incoming-list");
  const rows = (mesh.data?.incoming || []).filter((s) => ["accepted", "receiving"].includes(s.state) || (s.state === "done" && Date.now() - (mesh.incomingRows.get(s.id)?.doneAt || Date.now()) < 5000));
  const keep = new Set();
  for (const s of rows) {
    keep.add(s.id);
    let info = mesh.incomingRows.get(s.id);
    if (!info) {
      info = { el: h("li", { class: "job is-entering" }), doneAt: 0 };
      mesh.incomingRows.set(s.id, info);
      list.append(info.el);
    }
    if (s.state === "done" && !info.doneAt) info.doneAt = Date.now();
    const pct = s.total ? Math.min(100, (s.received / s.total) * 100) : s.state === "done" ? 100 : 0;
    const title = s.files.length === 1 ? s.files[0].name : plural(s.files.length, "file");
    const text = s.state === "done" ? `Received from ${s.from.name}` : s.state === "accepted" ? `Waiting for ${s.from.name}…` : `${formatBytes(s.received)} of ${formatBytes(s.total)} from ${s.from.name}`;
    info.el.dataset.state = s.state;
    info.el.replaceChildren(
      h(
        "div",
        { class: "job-head" },
        h("span", { class: "thumb is-incoming", "aria-hidden": "true" }, icon("download")),
        h(
          "div",
          { class: "row-main" },
          h("span", { class: "row-title", title }, title),
          h("span", { class: "row-sub" }, text),
          h("span", { class: "progress", role: "progressbar", "aria-label": `Receiving ${title}`, "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(pct)) }, h("span", { class: "progress-bar", style: { transform: `scaleX(${pct / 100})` } })),
        ),
        h(
          "div",
          { class: "row-actions is-visible" },
          s.state === "done"
            ? h("span", { class: "status-badge", "aria-label": "Received" }, icon("check"))
            : h("button", { class: "icon-btn", type: "button", title: "Stop", "aria-label": `Stop receiving ${title}`, onclick: () => stopIncoming(s.id) }, icon("x")),
        ),
      ),
    );
  }
  for (const [id, info] of mesh.incomingRows) {
    if (!keep.has(id)) {
      info.el.remove();
      if (!(mesh.data?.incoming || []).some((s) => s.id === id)) mesh.incomingRows.delete(id);
    }
  }
}

async function stopIncoming(id) {
  try {
    await api(`/api/incoming/${encodeURIComponent(id)}`, { method: "DELETE" });
  } catch (err) {
    toast(err.message, { tone: "error", icon: "alert" });
  }
  mesh.etag = null;
  ctx.requestPoll();
}

// ------------------------------------------------------------------ dialogs

let confirmAnswer = null; // resolves the question on screen

function answerConfirm(ok) {
  const resolve = confirmAnswer;
  confirmAnswer = null;
  resolve?.(ok);
}

export function confirmDialog({ title, text, items = [], confirm = "OK" }) {
  const dialog = $("#confirm-dialog");
  if (!dialog.dataset.wired) {
    // Answer from the button press itself (sync): a "close" event arrives later and
    // could otherwise land on the next question when two come in quick succession.
    dialog.dataset.wired = "1";
    dialog.querySelector("form").addEventListener("submit", (event) => answerConfirm(event.submitter?.value === "ok"));
    dialog.addEventListener("cancel", () => answerConfirm(false));
    dialog.addEventListener("close", () => !dialog.open && answerConfirm(dialog.returnValue === "ok"));
  }
  answerConfirm(false); // a question still waiting (shouldn't happen) is a no
  $("#confirm-title").textContent = title;
  $("#confirm-text").textContent = text;
  $("#confirm-items").replaceChildren(...items.map((name) => h("li", {}, name)));
  $("#confirm-ok").textContent = confirm;
  dialog.returnValue = "";
  return new Promise((resolve) => {
    confirmAnswer = resolve;
    if (!dialog.open) dialog.showModal();
  });
}

/** Text that looks like a password, code, card number or key asks first. */
export async function confirmSensitive(text, { action = "Send" } = {}) {
  const reason = looksSensitive(text);
  if (!reason) return true;
  return confirmDialog({
    title: `This looks like ${SENSITIVE_REASONS[reason]}`,
    text: `Make sure you trust everyone who’ll get it. ${action} it anyway?`,
    confirm: `${action} anyway`,
  });
}

/** One narrow firewall rule for this app, after Windows asks the owner (UAC). */
async function allowOnThisNetwork() {
  const button = $("#me-allow");
  button.disabled = true;
  try {
    const { data } = await api("/api/network/allow", { method: "POST" });
    if (data.result === "allowed") toast("Allowed — other devices can reach this PC now", { tone: "success", icon: "check" });
    else if (data.result === "failed" && mesh.data?.network?.blocking_rules?.length) toast("Couldn’t remove the blocking rule — Windows Security → Firewall can", { tone: "error", icon: "alert" });
    else if (data.result === "cancelled") toast("Nothing changed — Windows asked and the answer was No", { icon: "alert" });
    else toast("Couldn’t change the firewall rule", { tone: "error", icon: "alert" });
  } catch (err) {
    toast(err.message, { tone: "error", icon: "alert" });
  } finally {
    button.disabled = false;
  }
}

function setupRename() {
  const dialog = $("#rename-dialog");
  const input = $("#rename-input");
  $("#me-name").addEventListener("click", () => {
    input.value = mesh.data?.me.name || "";
    $("#rename-hint").textContent = mesh.data?.me.kind === "owner" ? "Nearby devices see this name." : "Others see this name while this page is open.";
    dialog.showModal();
    input.select();
  });
  $("#rename-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = input.value.trim();
    if (!name) return;
    try {
      const { data } = await api("/api/me", { method: "POST", json: { name } });
      if (mesh.data?.me.kind !== "owner") rememberIdentity({ ...guessDevice(), ...(storedIdentity() || {}), name: data.name });
      dialog.close();
      mesh.etag = null;
      ctx.requestPoll();
    } catch (err) {
      $("#rename-error").textContent = err.message;
    }
  });
}

// ----------------------------------------------------------------- pairing

function setTab(name) {
  for (const tab of document.querySelectorAll("#connect-dialog [role=tab]")) {
    const on = tab.dataset.tab === name;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
  }
  for (const panel of document.querySelectorAll("#connect-dialog [data-panel]")) panel.hidden = panel.dataset.panel !== name;
  if (name === "enter") requestAnimationFrame(() => $("#pair-input").focus());
  else stopScanner();
}

function formatCode(code) {
  return `${code.slice(0, 3)} ${code.slice(3)}`;
}

function fillPairing() {
  const pairing = mesh.data?.pairing;
  if (!pairing) return;
  // Until the app says the window is open, the code it has isn't the one that will work.
  $("#pair-code").textContent = pairing.open ? formatCode(pairing.code) : "––– –––";
  $("#pair-code").setAttribute("aria-label", pairing.open ? `Pairing code ${pairing.code.split("").join(" ")}` : "Getting a pairing code");
  const qr = $("#qr-image");
  const src = pairing.open ? `/api/pair/qr.svg?c=${encodeURIComponent(pairing.code)}` : "";
  if (!src) qr.removeAttribute("src");
  else if (qr.getAttribute("src") !== src) qr.src = src;
  const minutes = Math.max(1, Math.round(pairing.expires_in / 60));
  $("#pair-expiry-text").textContent = `Changes in ${minutes} min`;
}

// A code only works while Add device is open on its device: tell the app when
// the sheet opens, renew it while it stays open, and say when it closes.
function holdPairingOpen() {
  const renew = () =>
    api("/api/pair/open", { method: "POST" })
      .then(({ data }) => {
        if (mesh.data) mesh.data.pairing = { ...data, open: true };
        fillPairing();
      })
      .catch(() => {});
  clearInterval(mesh.pairTimer);
  renew();
  mesh.pairTimer = setInterval(() => ($("#connect-dialog").open ? renew() : releasePairing()), 20000);
}

function releasePairing() {
  if (!mesh.pairTimer) return;
  clearInterval(mesh.pairTimer);
  mesh.pairTimer = null;
  api("/api/pair/close", { method: "POST" }).catch(() => {});
}

export function openAddDevice({ tab = "show" } = {}) {
  const dialog = $("#connect-dialog");
  const owner = Boolean(state.info.owner);
  dialog.classList.toggle("is-owner", owner);
  $("#connect-tabs").hidden = !owner;
  $("#pair-code-block").hidden = !owner;
  $("#connect-lead").textContent = owner
    ? "Scan with a phone’s camera, or enter this code in Open Transfer on your other device."
    : "Scan with another phone’s camera, or open the link on any device on the same Wi-Fi.";
  const info = state.info;
  $("#share-url").textContent = info.share_url;
  if (owner) {
    fillPairing();
    holdPairingOpen();
  } else $("#qr-image").src = `/api/qr.svg?v=${encodeURIComponent(info.share_url)}`;
  $("#pin-note").hidden = !info.pin;
  $("#pin-note-value").textContent = info.pin || "";
  $("#share-url-button").hidden = !navigator.share;
  const others = (info.urls || []).filter((u) => u !== info.share_url);
  $("#other-urls").hidden = others.length === 0;
  $("#other-url-list").replaceChildren(...others.map((u) => h("li", {}, u)));
  $("#scan-button").hidden = !canScan();
  $("#pair-error").textContent = "";
  setTab(owner ? tab : "show");
  if (!dialog.open) dialog.showModal();
}

function canScan() {
  if (nativeApp()?.scanQr) return true;
  return Boolean(navigator.mediaDevices?.getUserMedia) && window.isSecureContext;
}

async function pair(code, address) {
  const submit = $("#pair-submit");
  const error = $("#pair-error");
  error.textContent = "";
  submit.classList.add("is-busy");
  submit.disabled = true;
  try {
    const { data } = await api("/api/pair", { method: "POST", json: { code, address: address || undefined } });
    mesh.paired?.add(data.device.id); // already announced; don't toast twice
    toast(`Paired with ${data.device.name}`, { tone: "success", icon: "link" });
    $("#connect-dialog").close();
    $("#pair-input").value = "";
    mesh.etag = null;
    ctx.requestPoll();
  } catch (err) {
    error.textContent = err.message;
    if (err.code === "code_not_found") $("#pair-address-wrap").open = true;
  } finally {
    submit.classList.remove("is-busy");
    submit.disabled = false;
  }
}

function handleScan(text) {
  stopScanner();
  if (!text) return;
  let url;
  try {
    url = new URL(text);
  } catch {
    $("#pair-error").textContent = "That QR code isn’t from Open Transfer.";
    return;
  }
  const code = url.searchParams.get("pair");
  if (!code) {
    $("#pair-error").textContent = "That QR code doesn’t include a pairing code. Show the code on the other device.";
    return;
  }
  setTab("enter");
  $("#pair-input").value = formatCode(code);
  $("#pair-address").value = url.host;
  pair(code, url.host);
}

let scanner = null;

const CAMERA_PROBLEMS = {
  NotAllowedError: "Camera access is off. Allow it in your browser or system settings, or enter the code instead.",
  SecurityError: "Camera access is off. Allow it in your browser or system settings, or enter the code instead.",
  NotFoundError: "No camera found. Enter the code shown on the other device.",
  OverconstrainedError: "No camera found. Enter the code shown on the other device.",
  NotReadableError: "The camera is in use by another app. Close it, or enter the code instead.",
};

/** A QR reader for video frames: the browser's own, or jsQR where there's none (Safari, Firefox, desktop windows). */
async function qrReader() {
  if ("BarcodeDetector" in window && (await window.BarcodeDetector.getSupportedFormats()).includes("qr_code")) {
    const detector = new window.BarcodeDetector({ formats: ["qr_code"] });
    return async (video) => (await detector.detect(video))[0]?.rawValue;
  }
  if (!window.jsQR) {
    await new Promise((resolve, reject) => {
      document.head.append(h("script", { src: "/static/vendor/jsQR.js", onload: resolve, onerror: reject }));
    });
  }
  const canvas = document.createElement("canvas");
  const g = canvas.getContext("2d", { willReadFrequently: true });
  return async (video) => {
    const scale = Math.min(1, 640 / Math.max(video.videoWidth, video.videoHeight));
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    if (!canvas.width) return undefined;
    g.drawImage(video, 0, 0, canvas.width, canvas.height);
    return window.jsQR(g.getImageData(0, 0, canvas.width, canvas.height).data, canvas.width, canvas.height)?.data;
  };
}

async function startScan() {
  const app = nativeApp();
  if (app?.scanQr) {
    app.scanQr();
    return;
  }
  if (scanner) return; // already starting or scanning
  const mine = { stream: null, stopped: false, cameras: [], camera: null };
  scanner = mine;
  $("#pair-error").textContent = "";
  try {
    const read = await qrReader();
    await openCamera(mine, { facingMode: "environment" });
    if (mine.stopped) return;
    $("#scan-view").hidden = false;
    mine.cameras = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
    $("#scan-switch").hidden = mine.stopped || mine.cameras.length < 2;
    const video = $("#scan-video");
    const tick = async () => {
      if (mine.stopped) return;
      try {
        const text = await read(video);
        if (text) {
          handleScan(text);
          return;
        }
      } catch {
        /* frame not ready */
      }
      setTimeout(tick, 100);
    };
    tick();
  } catch (err) {
    if (mine.stopped) return;
    stopScanner();
    $("#pair-error").textContent = CAMERA_PROBLEMS[err?.name] || "Couldn’t open the camera. Enter the code instead.";
    $("#pair-input").focus();
  }
}

/** Point the scanner at a camera, letting go of the one it had. */
async function openCamera(mine, video) {
  const stream = await navigator.mediaDevices.getUserMedia({ video });
  if (mine.stopped) {
    for (const track of stream.getTracks()) track.stop(); // closed while the camera was starting
    return;
  }
  for (const track of mine.stream?.getTracks() || []) track.stop();
  mine.stream = stream;
  mine.camera = stream.getVideoTracks()[0]?.getSettings().deviceId ?? video.deviceId?.exact;
  const el = $("#scan-video");
  el.srcObject = stream;
  await el.play();
}

function switchCamera() {
  const mine = scanner;
  if (!mine?.stream || mine.cameras.length < 2) return;
  const at = Math.max(0, mine.cameras.findIndex((d) => d.deviceId === mine.camera)); // unknown: the default, listed first
  const next = mine.cameras[(at + 1) % mine.cameras.length];
  openCamera(mine, { deviceId: { exact: next.deviceId } }).catch((err) => {
    if (mine.stopped) return;
    $("#pair-error").textContent = CAMERA_PROBLEMS[err?.name] || "Couldn’t switch cameras.";
  });
}

function stopScanner() {
  if (!scanner) return;
  scanner.stopped = true;
  for (const track of scanner.stream?.getTracks() || []) track.stop();
  const video = $("#scan-video");
  if (video) video.srcObject = null;
  scanner = null;
  $("#scan-view").hidden = true;
  $("#scan-switch").hidden = true;
}

function setupConnectSheet() {
  const dialog = $("#connect-dialog");
  for (const button of document.querySelectorAll(".js-add-device")) button.addEventListener("click", () => openAddDevice());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => {
    stopScanner();
    releasePairing();
  });
  for (const tab of document.querySelectorAll("#connect-dialog [role=tab]")) {
    tab.addEventListener("click", () => setTab(tab.dataset.tab));
    tab.addEventListener("keydown", (event) => {
      if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
        setTab(tab.dataset.tab === "show" ? "enter" : "show");
        $(`#connect-dialog [role=tab][aria-selected=true]`).focus();
      }
    });
  }
  $("#new-code").addEventListener("click", async () => {
    try {
      await api("/api/pair/new-code", { method: "POST" });
      mesh.etag = null;
      await fetchState();
      fillPairing();
    } catch (err) {
      toast(err.message, { tone: "error", icon: "alert" });
    }
  });
  $("#pair-input").addEventListener("input", (event) => {
    const digits = event.target.value.replace(/\D/g, "").slice(0, 6);
    event.target.value = digits.length > 3 ? formatCode(digits) : digits;
    $("#pair-error").textContent = "";
  });
  $("#pair-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const code = $("#pair-input").value.replace(/\D/g, "");
    if (code.length !== 6) {
      $("#pair-error").textContent = "Enter all 6 digits.";
      return;
    }
    pair(code, $("#pair-address").value.trim());
  });
  $("#scan-button").addEventListener("click", startScan);
  $("#scan-cancel").addEventListener("click", stopScanner);
  $("#scan-switch").addEventListener("click", switchCamera);
  document.addEventListener("visibilitychange", () => document.hidden && stopScanner());
  window.addEventListener("ot-native-scan", (event) => handleScan(event.detail));
  $("#copy-url").addEventListener("click", async () => {
    const button = $("#copy-url");
    const ok = await copyText(state.info.share_url);
    const label = button.querySelector("span");
    label.textContent = ok ? "Copied" : "Press ⌘C";
    if (!ok) {
      const range = document.createRange();
      range.selectNodeContents($("#share-url"));
      getSelection().removeAllRanges();
      getSelection().addRange(range);
    }
    setTimeout(() => {
      label.textContent = "Copy";
    }, 1600);
  });
  $("#share-url-button").addEventListener("click", async () => {
    try {
      await navigator.share({ title: "Open Transfer", text: `Send files to ${state.info.device}`, url: state.info.share_url });
    } catch {
      /* the user closed the share sheet */
    }
  });
  setupInvites();
}

let inviteCountdown = 0;

function setupInvites() {
  const block = $("#invite-block");
  if (!block) return;
  // Guest links are minted by the owner (this device's own window).
  const owner = Boolean(state.info.owner);
  block.hidden = !owner;
  if (!owner) return;
  const field = $("#invite-field");
  const urlEl = $("#invite-url");
  const expiryEl = $("#invite-expiry");
  const render = () => {
    if (!inviteCountdown) {
      expiryEl.textContent = "";
      return;
    }
    const left = Math.max(0, inviteCountdown - Math.round(performance.now() / 1000));
    const m = Math.floor(left / 60);
    const s = String(left % 60).padStart(2, "0");
    expiryEl.textContent = left ? `expires in ${m}:${s}` : "expired — make a new one";
  };
  setInterval(() => {
    if ($("#connect-dialog").open && inviteCountdown) render();
  }, 1000);
  $("#invite-new").addEventListener("click", async () => {
    const button = $("#invite-new");
    button.classList.add("is-busy");
    button.disabled = true;
    try {
      const { data } = await api("/api/invites", { method: "POST", json: {} });
      urlEl.textContent = data.url;
      field.hidden = false;
      button.querySelector("span").textContent = "New guest link";
      inviteCountdown = Math.round(performance.now() / 1000) + (data.expires_in || 600);
      render();
      toast("Guest link ready — it works once, then expires", { icon: "link" });
    } catch (err) {
      toast(err.message, { tone: "error", icon: "alert" });
    } finally {
      button.classList.remove("is-busy");
      button.disabled = false;
    }
  });
  $("#invite-copy").addEventListener("click", async () => {
    const ok = await copyText(urlEl.textContent);
    $("#invite-copy").querySelector("span").textContent = ok ? "Copied" : "Press ⌘C";
    setTimeout(() => {
      $("#invite-copy").querySelector("span").textContent = "Copy";
    }, 1600);
  });
}

// -------------------------------------------------------------------- setup

export function initNearby(context) {
  ctx = { ...ctx, ...context };
  setupConnectSheet();
  setupRename();
  setupTextDialog();
  $("#me-allow").addEventListener("click", allowOnThisNetwork);
  $("#select-all").addEventListener("click", () => {
    const online = onlineDevices();
    const allSelected = online.every((d) => mesh.selected.has(d.id));
    mesh.selected = allSelected ? new Set() : new Set(online.map((d) => d.id));
    renderDevices();
    renderSendBar();
  });
  $("#send-go").addEventListener("click", sendStaged);
  setupQuickSend();
  $("#send-clear").addEventListener("click", () => {
    mesh.staged = [];
    mesh.selected.clear();
    renderDevices();
    renderSendBar();
  });
  $("#send-pick").addEventListener("click", () => $("#file-input").click());
  $("#send-folder")?.addEventListener("click", () => $("#folder-input").click());
  $("#incoming-accept").addEventListener("click", () => decide(true));
  $("#incoming-decline").addEventListener("click", () => decide(false));
  $("#incoming-dialog").addEventListener("cancel", (event) => event.preventDefault()); // answer, don't dismiss
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && document.title.startsWith("●")) document.title = "Open Transfer";
  });
  setInterval(() => {
    if (mesh.data && !document.hidden) {
      renderDevices();
      renderIncomingRows();
    }
  }, 2000);
}

function setupQuickSend() {
  const everyone = $("#everyone-target");
  everyone.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = "copy";
    everyone.classList.add("is-drop-target");
    announceDrop(`Release to send to everyone nearby (${plural(onlineDevices().length, "device")})`);
  });
  everyone.addEventListener("dragleave", () => everyone.classList.remove("is-drop-target"));
  everyone.addEventListener("drop", (event) => {
    everyone.classList.remove("is-drop-target");
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.stopPropagation();
    document.dispatchEvent(new CustomEvent("ot-drop-handled"));
    sendDropped(event, onlineDevices().map((d) => d.id));
  });
  document.addEventListener("ot-drag-end", () => announceDrop(""));

  const input = $("#quick-input");
  input.addEventListener("change", () => {
    const files = [...(input.files || [])];
    const id = mesh.quickTarget;
    input.value = "";
    mesh.quickTarget = null;
    if (!files.length || !id) return;
    if (!onlineDevices().some((d) => d.id === id)) {
      toast(`${deviceName(id)} isn’t nearby right now — nothing was sent`, { tone: "error", icon: "alert" });
      return;
    }
    startSend([id], files);
  });
  // Alt+1…9: quick send to the first…ninth favorite that's nearby.
  document.addEventListener("keydown", (event) => {
    const digit = /^Digit([1-9])$/.exec(event.code);
    if (!digit || !event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (event.target.closest?.("input, textarea, [contenteditable]") || document.querySelector("dialog[open]")) return;
    const target = onlineFavorites()[Number(digit[1]) - 1];
    if (!target) return;
    event.preventDefault();
    quickSend(target.id);
  });
}

export function isDeviceMode() {
  return Boolean(state.info.owner) || !state.info.permissions?.upload;
}

export { ApiError };
