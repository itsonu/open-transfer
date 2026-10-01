// Open Transfer — nearby devices: the device grid, choosing recipients,
// sending, incoming "Accept?" prompts, pairing (show / enter / scan a code)
// and renaming this device.

import { $, ApiError, api, copyText, formatBytes, formatDuration, h, icon, plural, state, toast } from "./lib.js";

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
  tiles: new Map(), // device id -> { el, sig }
  incomingRows: new Map(),
};

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

export function busy() {
  return [...mesh.jobs.values()].some((j) => j.state === "waiting" || j.state === "uploading") || mesh.promptQueue.length > 0;
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
  if (!state.info.owner) ctx.libraryChanged(data.inbox || []);
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
  if (me.kind === "owner") {
    status.textContent = discovery ? "Visible to nearby devices" : "Not discoverable — add devices by code or address";
    status.dataset.tone = discovery ? "ok" : "warn";
  } else {
    status.textContent = me.paired ? `Paired with ${host.name}` : `Connected through ${host.name}`;
    status.dataset.tone = "ok";
  }
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
    const { target, view, job } = activity;
    const pct = view.total ? Math.min(100, Math.round((target.sent / view.total) * 100)) : 0;
    switch (target.state) {
      case "offering":
      case "waiting":
        return { text: "Waiting…", tone: "accent", ring: "spin" };
      case "accepted":
        return job.state === "uploading" ? { text: "Sending…", tone: "accent", ring: 0 } : { text: "Accepted", tone: "accent", ring: 0 };
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
  );
  const button = el.querySelector(".device-button");
  button.addEventListener("click", () => toggle(d.id));
  // Drop files straight onto a device to send them to it — the AirDrop gesture.
  el.addEventListener("dragover", (event) => {
    if (!el.dataset.online || ![...(event.dataTransfer?.types || [])].includes("Files")) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = "copy";
    el.classList.add("is-drop-target");
  });
  el.addEventListener("dragleave", () => el.classList.remove("is-drop-target"));
  el.addEventListener("drop", (event) => {
    el.classList.remove("is-drop-target");
    if (!el.dataset.online) return;
    event.preventDefault();
    event.stopPropagation();
    document.dispatchEvent(new CustomEvent("ot-drop-handled"));
    const files = ctx.droppedFiles(event);
    if (files.length) startSend([el.dataset.id], files);
  });
  return el;
}

function updateTile(el, d) {
  const selected = mesh.selected.has(d.id);
  const status = tileStatus(d);
  el.dataset.online = d.online ? "1" : "";
  el.dataset.kind = d.kind;
  el.classList.toggle("is-selected", selected);
  el.classList.toggle("is-offline", !d.online);
  el.classList.toggle("is-paired", Boolean(d.paired));
  el.dataset.tone = status.tone;
  const button = el.querySelector(".device-button");
  button.disabled = !d.online;
  button.setAttribute("aria-pressed", String(selected));
  button.setAttribute("aria-label", `${d.name}, ${status.text}${d.paired ? ", paired" : ""}${selected ? ", selected" : ""}`);
  button.title = d.address ? `${d.name} · ${d.address}` : d.name;
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
  const devices = [...mesh.data.devices].sort((a, b) => Number(b.online) - Number(a.online));
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

function renderSendBar() {
  const bar = $("#send-bar");
  const files = mesh.staged;
  const ids = [...mesh.selected];
  bar.hidden = !files.length && !ids.length;
  document.body.classList.toggle("has-send-bar", !bar.hidden);
  if (bar.hidden) return;
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

async function sendStaged() {
  const ids = [...mesh.selected];
  const files = [...mesh.staged];
  if (!ids.length || !files.length) return;
  const online = onlineDevices();
  const everyone = ids.length >= 2 && ids.length === online.length;
  if (ids.length >= 3 || everyone) {
    const ok = await confirmDialog({
      title: everyone ? `Send to everyone nearby?` : `Send to ${ids.length} devices?`,
      text: `${plural(files.length, "file")} (${formatBytes(files.reduce((s, f) => s + f.size, 0))}) will be offered to:`,
      items: ids.map(deviceName),
      confirm: `Send to ${ids.length}`,
    });
    if (!ok) return;
  }
  await startSend(ids, files);
}

async function startSend(ids, files) {
  let data;
  try {
    ({ data } = await api("/api/send", {
      method: "POST",
      json: { to: ids, files: files.map((f) => ({ name: f.name || "Untitled", size: f.size, mime: f.type || "application/octet-stream" })) },
    }));
  } catch (err) {
    toast(err.message, { tone: "error", icon: "alert" });
    return;
  }
  const job = { id: data.job.id, files, targets: ids, state: "waiting", index: 0, uploaded: 0, speed: 0, xhr: null, finishedAt: 0 };
  mesh.jobs.set(job.id, job);
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
    if (job.state !== "waiting") continue;
    const view = serverJob(job.id);
    if (!view) {
      if (mesh.data) finishJob(job, "failed");
      continue;
    }
    const states = view.targets.map((t) => t.state);
    if (states.some((s) => s === "offering" || s === "waiting")) continue;
    if (states.some((s) => s === "accepted")) uploadNext(job);
    else finishJob(job, "nobody");
  }
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
  if (outcome === "nobody") {
    if (declined.length === targets.length && targets.length) toast(targets.length === 1 ? `${targets[0].name} declined` : "Everyone declined", { tone: "error", icon: "x" });
    else if (failed.length) toast(failed[0].reason || `Couldn’t send to ${failed[0].name}`, { tone: "error", icon: "alert" });
  } else if (done.length && !failed.length && !declined.length) {
    toast(done.length === 1 ? `Sent to ${done[0].name}` : `Sent to ${plural(done.length, "device")}`, { tone: "success", icon: "check" });
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
  if (job.index >= job.files.length) {
    job.state = "uploaded";
    setTimeout(() => {
      const view = serverJob(job.id);
      const states = view?.targets.map((t) => t.state) || [];
      if (!states.some((s) => ACTIVE.has(s))) finishJob(job, "done");
    }, 400);
    ctx.requestPoll();
    return;
  }
  job.state = "uploading";
  const file = job.files[job.index];
  const xhr = new XMLHttpRequest();
  job.xhr = xhr;
  job.lastAt = performance.now();
  job.lastLoaded = 0;
  job.fileLoaded = 0;
  xhr.open("PUT", `/api/send/${encodeURIComponent(job.id)}/files/${job.index}`);
  xhr.responseType = "json";
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
    } else {
      const message = xhr.response?.error?.message || `Sending failed (${xhr.status || "no response"}).`;
      job.error = message;
      finishJob(job, "failed");
      if (xhr.status !== 410) toast(message, { tone: "error", icon: "alert" });
    }
    ctx.requestPoll();
  });
  xhr.addEventListener("error", () => {
    job.xhr = null;
    job.error = "Connection lost. Check your Wi-Fi and try again.";
    finishJob(job, "failed");
    toast(job.error, { tone: "error", icon: "alert" });
  });
  xhr.addEventListener("abort", () => {
    job.xhr = null;
  });
  xhr.send(file);
}

async function cancelJob(job) {
  job.state = "finished";
  job.finishedAt = Date.now();
  job.xhr?.abort();
  try {
    await api(`/api/send/${encodeURIComponent(job.id)}`, { method: "DELETE" });
  } catch {
    /* already gone */
  }
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

function retryJob(job) {
  const view = serverJob(job.id);
  const online = new Set(onlineDevices().map((d) => d.id));
  const ids = (view?.targets || []).filter((t) => ["failed", "expired", "canceled"].includes(t.state) && online.has(t.id)).map((t) => t.id);
  if (!ids.length) {
    toast("Those devices aren’t nearby right now.", { tone: "error", icon: "alert" });
    return;
  }
  mesh.jobs.delete(job.id);
  startSend(ids, job.files);
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
    if (job.state === "waiting") headline = `${formatBytes(view.total)} · waiting for ${plural(view.targets.filter((t) => t.state === "waiting" || t.state === "offering").length, "answer")}`;
    else if (job.state === "uploading") {
      const eta = job.speed > 0 ? formatDuration((total - own) / job.speed) : "";
      headline = [`${formatBytes(own)} of ${formatBytes(view.total)}`, job.speed > 0 && `${formatBytes(job.speed)}/s`, eta].filter(Boolean).join(" · ");
    } else headline = formatBytes(view.total);
    const anyWaiting = view.targets.some((t) => t.state === "waiting" || t.state === "offering");
    const anyAccepted = view.targets.some((t) => t.state === "accepted");
    const retryable = view.targets.some((t) => ["failed", "expired"].includes(t.state));
    const active = job.state === "waiting" || job.state === "uploading" || job.state === "uploaded";
    row.replaceChildren(
      h(
        "div",
        { class: "job-head" },
        h("span", { class: "thumb", "aria-hidden": "true" }, icon("send")),
        h("div", { class: "row-main" }, h("span", { class: "row-title", title }, title), h("span", { class: "row-sub" }, headline)),
        h(
          "div",
          { class: "row-actions is-visible" },
          anyWaiting && anyAccepted && job.state === "waiting" && h("button", { class: "btn btn-tinted btn-small", type: "button", onclick: () => skipWaiting(job) }, "Send now"),
          !active && retryable && h("button", { class: "icon-btn is-accent", type: "button", title: "Retry", "aria-label": `Retry sending ${title}`, onclick: () => retryJob(job) }, icon("retry")),
          h(
            "button",
            {
              class: "icon-btn",
              type: "button",
              title: active ? "Cancel" : "Dismiss",
              "aria-label": `${active ? "Cancel" : "Dismiss"} ${title}`,
              onclick: () => (active ? cancelJob(job) : (mesh.jobs.delete(job.id), renderJobs(), renderDevices())),
            },
            icon("x"),
          ),
        ),
      ),
      h(
        "ul",
        { class: "job-targets" },
        view.targets.map((t) => {
          const pct = t.state === "done" ? 100 : Math.min(100, (t.sent / total) * 100);
          const text = t.state === "sending" ? `${Math.floor(pct)}%` : t.reason && FINAL.has(t.state) && t.state !== "done" ? t.reason : TARGET_TEXT[t.state] || t.state;
          return h(
            "li",
            { class: "job-target", dataset: { state: t.state } },
            h("span", { class: "mini-avatar", "aria-hidden": "true" }, icon(deviceIcon(t))),
            h("span", { class: "job-target-name" }, t.name),
            h("span", { class: "job-target-state" }, text),
            h("span", { class: "progress", role: "progressbar", "aria-label": `${t.name}: ${text}`, "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(pct)) }, h("span", { class: "progress-bar", style: { transform: `scaleX(${pct / 100})` } })),
          );
        }),
      ),
    );
  }
  for (const row of [...list.children]) if (!keep.has(row.dataset.job)) row.remove();
  renderIncomingRows();
  const any = list.children.length + $("#incoming-list").children.length;
  $("#activity").hidden = any === 0;
  const sending = [...mesh.jobs.values()].filter((j) => j.state === "uploading" || j.state === "waiting");
  if (sending.length && !document.title.startsWith("●")) document.title = "Sending · Open Transfer";
  else if (!sending.length && document.title === "Sending · Open Transfer") document.title = "Open Transfer";
}

window.addEventListener("beforeunload", (event) => {
  if ([...mesh.jobs.values()].some((j) => j.state === "uploading" || j.state === "waiting")) {
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
    }
    if (before && before !== s.state) {
      const from = s.from.name;
      if (s.state === "done") {
        toast(s.files.length === 1 ? `Received “${s.files[0].saved_name || s.files[0].name}” from ${from}` : `Received ${s.files.length} files from ${from}`, {
          tone: "success",
          icon: "check",
          duration: 5000,
          action: !owner && s.files.length === 1 ? { label: "Save", run: () => saveInboxFile(s.files[0].saved_name || s.files[0].name) } : undefined,
        });
        ctx.libraryChanged(null);
      } else if (s.state === "canceled" && before !== "pending") toast(`${from} stopped sending`, { tone: "error", icon: "x" });
      else if (s.state === "failed") toast(`Couldn’t receive files from ${from}`, { tone: "error", icon: "alert" });
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

function showNextPrompt() {
  const dialog = $("#incoming-dialog");
  const current = mesh.promptId && mesh.data.incoming.find((s) => s.id === mesh.promptId);
  if (mesh.promptId && (!current || current.state !== "pending")) {
    if (current && current.state === "expired") toast(`The transfer from ${current.from.name} expired`, { icon: "alert" });
    if (current && current.state === "canceled") toast(`${current.from.name} canceled`, { icon: "x" });
    mesh.promptId = null;
    if (dialog.open) dialog.close();
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
    if (!dialog.open) dialog.showModal();
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
    ...shown.map((f) => h("li", {}, h("span", { class: "incoming-file-name" }, f.name), h("span", { class: "incoming-file-size" }, formatBytes(f.size)))),
    ...(s.files.length > shown.length ? [h("li", { class: "muted" }, `and ${s.files.length - shown.length} more`)] : []),
  );
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
  try {
    await api(`/api/incoming/${encodeURIComponent(id)}/${accept ? "accept" : "decline"}`, { method: "POST" });
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

export function confirmDialog({ title, text, items = [], confirm = "OK" }) {
  const dialog = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  $("#confirm-text").textContent = text;
  $("#confirm-items").replaceChildren(...items.map((name) => h("li", {}, name)));
  $("#confirm-ok").textContent = confirm;
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
  });
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
  $("#pair-code").textContent = formatCode(pairing.code);
  $("#pair-code").setAttribute("aria-label", `Pairing code ${pairing.code.split("").join(" ")}`);
  const qr = $("#qr-image");
  const src = `/api/pair/qr.svg?c=${pairing.code}`;
  if (qr.getAttribute("src") !== src) qr.src = src;
  const minutes = Math.max(1, Math.round(pairing.expires_in / 60));
  $("#pair-expiry-text").textContent = `Changes in ${minutes} min`;
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
  if (owner) fillPairing();
  else $("#qr-image").src = `/api/qr.svg?v=${encodeURIComponent(info.share_url)}`;
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
  return "BarcodeDetector" in window && Boolean(navigator.mediaDevices?.getUserMedia) && window.isSecureContext;
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

async function startScan() {
  const app = nativeApp();
  if (app?.scanQr) {
    app.scanQr();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
    const video = $("#scan-video");
    video.srcObject = stream;
    $("#scan-view").hidden = false;
    await video.play();
    const detector = new window.BarcodeDetector({ formats: ["qr_code"] });
    scanner = { stream, stopped: false };
    const tick = async () => {
      if (!scanner || scanner.stopped) return;
      try {
        const codes = await detector.detect(video);
        if (codes.length) {
          handleScan(codes[0].rawValue);
          return;
        }
      } catch {
        /* frame not ready */
      }
      requestAnimationFrame(tick);
    };
    tick();
  } catch {
    $("#pair-error").textContent = "Couldn’t open the camera. Enter the code instead.";
  }
}

function stopScanner() {
  if (!scanner) return;
  scanner.stopped = true;
  for (const track of scanner.stream.getTracks()) track.stop();
  scanner = null;
  $("#scan-view").hidden = true;
}

function setupConnectSheet() {
  const dialog = $("#connect-dialog");
  for (const button of document.querySelectorAll(".js-add-device")) button.addEventListener("click", () => openAddDevice());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", stopScanner);
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
}

// -------------------------------------------------------------------- setup

export function initNearby(context) {
  ctx = { ...ctx, ...context };
  setupConnectSheet();
  setupRename();
  $("#select-all").addEventListener("click", () => {
    const online = onlineDevices();
    const allSelected = online.every((d) => mesh.selected.has(d.id));
    mesh.selected = allSelected ? new Set() : new Set(online.map((d) => d.id));
    renderDevices();
    renderSendBar();
  });
  $("#send-go").addEventListener("click", sendStaged);
  $("#send-clear").addEventListener("click", () => {
    mesh.staged = [];
    mesh.selected.clear();
    renderDevices();
    renderSendBar();
  });
  $("#send-pick").addEventListener("click", () => $("#file-input").click());
  $("#incoming-accept").addEventListener("click", () => decide(true));
  $("#incoming-decline").addEventListener("click", () => decide(false));
  $("#incoming-dialog").addEventListener("cancel", (event) => event.preventDefault()); // answer, don't dismiss
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && document.title.startsWith("●")) document.title = "Open Transfer";
  });
  setInterval(() => {
    if (mesh.data) {
      renderDevices();
      renderIncomingRows();
    }
  }, 2000);
}

export function isDeviceMode() {
  return Boolean(state.info.owner) || !state.info.permissions?.upload;
}

export { ApiError };
