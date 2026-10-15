// Open Transfer — client
// Plain ES modules, no build step. This file: views · connection & polling ·
// file list · shared-folder uploads · drag/drop/paste · PIN · boot.
// lib.js has the helpers, nearby.js the devices, sending and pairing.

import { $, api, can, formatBytes, formatDuration, guessKind, h, icon, plural, relativeTime, setLockHandler, state, toast } from "./lib.js";
import { announceSelf, busy, fetchState, forgetDirectFile, initNearby, isDeviceMode, mesh, openAddDevice, stageFiles } from "./nearby.js";

const POLL_MS = 1500;
const POLL_BUSY_MS = 700;
const POLL_HIDDEN_MS = 10000;
const MAX_PARALLEL_UPLOADS = 3;
const THUMB_MAX_BYTES = 25_000_000;
const THUMB_MIME = new Set(["image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp"]);
const PREVIEW_MIME = new Set([...THUMB_MIME, "video/mp4", "video/webm", "audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav"]);
const KIND_ICON = {
  image: "image", video: "video", audio: "audio", archive: "archive", document: "doc",
  spreadsheet: "table", presentation: "slides", code: "code", app: "app", other: "file",
};

Object.assign(state, {
  files: [],
  etag: null,
  loaded: false,
  online: null,
  failures: 0,
  search: "",
  hidden: new Set(), // optimistically deleted names
  justAdded: new Set(), // names to highlight once they appear
  transfers: new Map(),
  pollTimer: 0,
  pollGeneration: 0,
  locked: false,
  sentInBatch: [], // names sent since the last "Sent N files" toast
  started: false,
});

// "owner": this device's own window — its received files.
// "shared": a visitor in shared-folder mode — the folder everyone sees.
// "inbox": a visitor — files that were sent to them.
const libraryMode = () => (state.info.owner ? "owner" : can("browse") ? "shared" : "inbox");
const inInbox = () => libraryMode() === "inbox";
const fileUrl = (name, inline = false) =>
  inInbox() ? `/api/inbox/files/${encodeURIComponent(name)}` : `/files/${encodeURIComponent(name)}${inline ? "?inline=1" : ""}`;
const nativeApp = () => window.OpenTransferAndroid;

// ------------------------------------------------------------------ views

function showView(name) {
  for (const id of ["lock-view", "main-view", "fatal-view"]) {
    $(`#${id}`).hidden = id !== `${name}-view`;
  }
}

function applyInfo(info) {
  state.info = info;
  for (const el of document.querySelectorAll(".device-name")) el.textContent = info.device;
  const authed = info.auth.authenticated;
  $("#connect-button").hidden = !authed;
  $("#pin-input").inputMode = info.auth.numeric ? "numeric" : "text";
  if (!authed) return;

  const mode = libraryMode();
  const deviceMode = isDeviceMode();
  $("#send-section").hidden = !(can("send") || can("upload"));
  $("#empty-connect").hidden = false;
  $("#drop-title-pointer").textContent = deviceMode ? "Drop files to send" : "Drop files to share";
  $("#drop-title-touch").textContent = deviceMode ? "Send files" : "Share files";
  const hints = [];
  if (deviceMode) hints.push("Then choose who gets them — or drop files straight onto a device.");
  else if (!info.auth.required) hints.push("Everyone on this network with the link can see shared files.");
  const max = info.limits?.max_upload_size;
  if (max) hints.push(`Up to ${formatBytes(max)} per file.`);
  $("#drop-hint").textContent = hints.join(" ");
  $("#overlay-text").textContent = deviceMode ? "Drop to send" : `Drop to share with everyone`;
  $("#library-title").textContent = { owner: "Received", shared: "Shared files", inbox: "Sent to you" }[mode];
  $("#empty-title").textContent = mode === "shared" ? "No files yet" : "Nothing received yet";
  $("#empty-text").hidden = false;
  $("#empty-text").textContent =
    mode === "owner"
      ? `Files that people send to ${info.device} are saved in its Open Transfer folder and show up here.`
      : mode === "inbox"
        ? "Files that someone sends to this browser show up here, ready to save."
        : can("upload")
          ? `Files you send — or that others send to ${info.device} — will show up here.`
          : `Nothing is being shared from ${info.device} right now.`;
}

function showLock() {
  state.locked = true;
  state.online = null; // so unlocking shows "Connected" again
  stopPolling();
  showView("lock");
  $("#send-bar").hidden = true;
  setConnection("online", `Locked · ${state.info.device}`);
  $("#connect-button").hidden = true;
  requestAnimationFrame(() => $("#pin-input").focus());
}

function enterMain() {
  state.locked = false;
  showView("main");
  if (libraryMode() === "inbox" && state.info.shared_folder) {
    // A shared folder in receive-only mode: say where files go.
    $("#receive-only").hidden = false;
  }
  if (!state.started) {
    state.started = true;
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) poll(true);
    });
    setInterval(updateRelativeTimes, 30_000);
    announceSelf().then(() => poll(true));
    return;
  }
  poll(true);
}

// --------------------------------------------------- connection & polling

function setConnection(kind, text) {
  const el = $("#connection");
  el.dataset.state = kind;
  $("#connection-text").textContent = text;
}

function markOnline(online) {
  if (online === state.online) return;
  const wasOffline = state.online === false;
  state.online = online;
  if (online) {
    state.failures = 0;
    setConnection("online", state.info.owner ? "Ready" : `Connected to ${state.info.device}`);
    if (wasOffline) toast("Reconnected", { tone: "success", icon: "check" });
  } else {
    setConnection("offline", "Reconnecting…");
  }
}

function stopPolling() {
  clearTimeout(state.pollTimer);
}

async function poll(immediate = false) {
  stopPolling();
  const generation = ++state.pollGeneration;
  if (immediate) await refresh();
  // A newer poll() started while we awaited, or the page locked: let it own the timer.
  if (generation !== state.pollGeneration || state.locked) return;
  const delay = document.hidden
    ? POLL_HIDDEN_MS
    : state.online === false
      ? Math.min(1000 * 2 ** state.failures, 10_000)
      : busy()
        ? POLL_BUSY_MS
        : POLL_MS;
  state.pollTimer = setTimeout(() => poll(true), delay);
}

async function refresh() {
  try {
    await fetchState();
    if (libraryMode() !== "inbox") {
      const headers = state.etag ? { "If-None-Match": `"${state.etag}"` } : {};
      const result = await api("/api/files", { headers });
      if (!result.notModified) {
        state.files = result.data.files;
        state.totalSize = result.data.total_size;
        state.etag = (result.res.headers.get("ETag") || "").replace(/^W\//, "").replace(/"/g, "") || null;
        const names = new Set(state.files.map((f) => f.name));
        for (const name of state.hidden) if (!names.has(name)) state.hidden.delete(name);
        renderFiles();
      }
    }
    markOnline(true);
    if (!$("#fatal-view").hidden) showView("main");
  } catch (err) {
    if (err.status === 401) return;
    state.failures += 1;
    markOnline(false);
    if (!state.loaded && !mesh.data && state.failures >= 2) {
      showView("fatal");
      $("#fatal-text").textContent = err.status
        ? err.message
        : "Make sure Open Transfer is still running and that you’re on the same Wi-Fi network.";
    }
  }
}

// -------------------------------------------------------------- file list

const rows = new Map(); // name -> { el, sig }

function buildFileRow(file) {
  // Files received straight from another browser live in this page (a blob: URL).
  const href = file.url || fileUrl(file.name);
  const previewable = PREVIEW_MIME.has(file.mime);
  const thumb = h(
    previewable ? "a" : "span",
    previewable
      ? { class: "thumb thumb-link", href: file.url || fileUrl(file.name, true), target: "_blank", rel: "noopener", tabindex: "-1", "aria-hidden": "true" }
      : { class: "thumb", "aria-hidden": "true" },
    icon(KIND_ICON[file.kind] || "file"),
  );
  thumb.style.setProperty("--kind", `var(--k-${file.kind})`);
  if (THUMB_MIME.has(file.mime) && file.size <= THUMB_MAX_BYTES) {
    const img = h("img", { src: file.url || fileUrl(file.name, true), alt: "", loading: "lazy", decoding: "async" });
    img.addEventListener("load", () => img.classList.add("is-loaded"), { once: true });
    img.addEventListener("error", () => img.remove(), { once: true });
    thumb.append(img);
  }
  const sub = h("span", { class: "row-sub" });
  if (file.direct) sub.dataset.direct = "1";
  const openNative = nativeApp()?.openFile && libraryMode() === "owner";
  const actions = h(
    "div",
    { class: "row-actions" },
    openNative
      ? h("button", { class: "icon-btn is-accent", type: "button", "aria-label": `Open ${file.name}`, title: "Open", onclick: () => nativeApp().openFile(file.name) }, icon("download"))
      : h("a", { class: "icon-btn is-accent", href, download: file.name, "aria-label": `Download ${file.name}`, title: "Download" }, icon("download")),
    (can("delete") || inInbox()) &&
      h(
        "button",
        { class: "icon-btn is-danger", type: "button", "aria-label": `Delete ${file.name}`, title: "Delete", onclick: () => deleteFile(file, row) },
        icon("trash"),
      ),
  );
  const row = h(
    "li",
    { class: "row file-row", dataset: { name: file.name } },
    thumb,
    h("div", { class: "row-main" }, h("a", { class: "row-title", href, download: file.name, title: file.name }, file.name), sub),
    actions,
  );
  if (openNative) {
    row.querySelector(".row-title").addEventListener("click", (event) => {
      event.preventDefault();
      nativeApp().openFile(file.name);
    });
  }
  if (file.direct) {
    for (const link of row.querySelectorAll("a[download]")) {
      link.addEventListener("click", () => {
        file.saved = true;
        updateRowTime(row);
      });
    }
  }
  row._file = file;
  updateRowTime(row);
  return row;
}

function updateRowTime(row) {
  const f = row._file;
  const where = f.direct ? (f.saved ? " · saved" : " · in this browser — save it") : "";
  row.querySelector(".row-sub").textContent = `${formatBytes(f.size)} · ${relativeTime(f.modified)}${where}`;
}

function updateRelativeTimes() {
  for (const { el } of rows.values()) updateRowTime(el);
}

function renderFiles() {
  const list = $("#file-list");
  if (!state.loaded) {
    list.replaceChildren();
    list.setAttribute("aria-busy", "false");
    state.loaded = true;
  }
  const all = state.files.filter((f) => !state.hidden.has(f.name));
  const term = state.search.trim().toLowerCase();
  const visible = term ? all.filter((f) => f.name.toLowerCase().includes(term)) : all;
  const allNames = new Set(all.map((f) => f.name));

  let cursor = list.firstElementChild;
  for (const file of visible) {
    const sig = `${file.size}:${file.modified}`;
    let entry = rows.get(file.name);
    if (!entry || entry.sig !== sig) {
      const el = buildFileRow(file);
      if (entry) {
        if (cursor === entry.el) cursor = el; // keep our place in the list
        entry.el.replaceWith(el);
      }
      entry = { el, sig };
      rows.set(file.name, entry);
      if (state.justAdded.delete(file.name)) el.classList.add("is-new");
      else if (state.renderedOnce) el.classList.add("is-entering");
    }
    if (entry.el !== cursor) list.insertBefore(entry.el, cursor);
    else cursor = cursor.nextElementSibling;
  }
  // Anything after the cursor is no longer visible.
  const visibleNames = new Set(visible.map((f) => f.name));
  for (const [name, entry] of rows) {
    if (!visibleNames.has(name) && !entry.el.classList.contains("is-leaving")) entry.el.remove();
    if (!allNames.has(name) && !entry.el.classList.contains("is-leaving")) rows.delete(name);
  }
  state.renderedOnce = true;

  const total = all.reduce((sum, f) => sum + f.size, 0);
  $("#library-meta").textContent = all.length ? `${plural(all.length, "item")} · ${formatBytes(total)}` : "";
  $("#empty").hidden = all.length > 0;
  $("#dropzone").classList.toggle("is-compact", all.length > 0);
  list.hidden = visible.length === 0;
  $("#no-results").hidden = !(term && all.length && visible.length === 0);
  $("#no-results-term").textContent = state.search.trim();
  $("#search-wrap").hidden = all.length < 6 && !term;
  const download = $("#download-all");
  download.hidden = all.length < 2 || (term && visible.length === 0);
  const query = term ? visible.map((f) => `name=${encodeURIComponent(f.name)}`).join("&") : "";
  download.href = inInbox() ? "/api/inbox/archive" : `/api/archive${query ? `?${query}` : ""}`;
  download.querySelector("span").textContent = term ? `Download ${visible.length}` : "Download all";
}

async function deleteFile(file, row) {
  if (state.hidden.has(file.name)) return; // already being deleted (double click)
  state.hidden.add(file.name);
  row.classList.add("is-leaving");
  const timer = setTimeout(() => {
    row.remove();
    if (rows.get(file.name)?.el === row) rows.delete(file.name);
    renderFiles();
  }, 260);
  try {
    if (file.direct) {
      forgetDirectFile(file.name);
      toast(`Removed “${file.name}”`, { icon: "trash" });
      return;
    }
    if (inInbox()) {
      await api(`/api/inbox/files/${encodeURIComponent(file.name)}`, { method: "DELETE" });
      mesh.etag = null;
      toast(`Removed “${file.name}”`, { icon: "trash" });
      return;
    }
    const { data } = await api(`/api/files/${encodeURIComponent(file.name)}`, { method: "DELETE" });
    state.etag = null;
    toast(`Deleted “${file.name}”`, {
      icon: "trash",
      duration: Math.max(3000, Math.min(8000, data.undo_seconds * 1000 - 2000)),
      action: { label: "Undo", run: () => restoreFile(data.undo_token, file.name) },
    });
  } catch (err) {
    clearTimeout(timer);
    state.hidden.delete(file.name);
    row.classList.remove("is-leaving");
    renderFiles();
    toast(err.status === 404 ? `“${file.name}” was already removed.` : err.message, { tone: "error", icon: "alert" });
    if (err.status === 404) poll(true);
  }
}

async function restoreFile(token, name) {
  try {
    const { data } = await api(`/api/trash/${encodeURIComponent(token)}/restore`, { method: "POST" });
    state.hidden.delete(name);
    state.justAdded.add(data.file.name);
    state.etag = null;
    await poll(true);
    toast(`Restored “${data.file.name}”`, { tone: "success", icon: "check" });
  } catch (err) {
    toast(err.message, { tone: "error", icon: "alert" });
  }
}

// ---------------------------------------------------------------- uploads

let transferSeq = 0;
let renderQueued = false;

function pastedName(file, source) {
  const name = file.name || "Untitled";
  if (source === "paste" && /^image\.\w+$/i.test(name)) {
    const stamp = new Date().toISOString().slice(0, 19).replace("T", " ").replace(/:/g, ".");
    return `Pasted image ${stamp}.${name.split(".").pop()}`;
  }
  return name;
}

function enqueueFiles(fileList, { source = "picker" } = {}) {
  const files = [...fileList];
  if (!files.length) return;
  if (isDeviceMode()) {
    if (!can("send")) {
      toast("Sending files is turned off on this device.", { tone: "error", icon: "alert" });
      return;
    }
    stageFiles(files.map((f) => (pastedName(f, source) === f.name ? f : new File([f], pastedName(f, source), { type: f.type }))));
    return;
  }
  if (!can("upload")) {
    toast("Sending files is turned off on this computer.", { tone: "error", icon: "alert" });
    return;
  }
  const max = state.info.limits?.max_upload_size || 0;
  for (const file of files) {
    const name = pastedName(file, source);
    const t = { id: ++transferSeq, file, name, size: file.size, loaded: 0, state: "queued", speed: 0, error: "" };
    if (max && file.size > max) {
      t.state = "error";
      t.error = `Larger than the ${formatBytes(max)} limit`;
      t.fatal = true;
    }
    state.transfers.set(t.id, t);
    t.el = buildTransferRow(t);
    $("#transfer-list").append(t.el);
  }
  $("#transfers").hidden = false;
  pump();
  renderTransfers();
}

function pump() {
  let active = [...state.transfers.values()].filter((t) => t.state === "uploading").length;
  for (const t of state.transfers.values()) {
    if (active >= MAX_PARALLEL_UPLOADS) break;
    if (t.state === "queued") {
      startUpload(t);
      active += 1;
    }
  }
}

function startUpload(t) {
  Object.assign(t, { state: "uploading", loaded: 0, speed: 0, error: "", lastAt: performance.now(), lastLoaded: 0 });
  const xhr = new XMLHttpRequest();
  t.xhr = xhr;
  xhr.open("POST", "/api/files");
  xhr.responseType = "json";
  xhr.setRequestHeader("Accept", "application/json");
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.setRequestHeader("X-Filename", encodeURIComponent(t.name));
  xhr.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) return;
    const now = performance.now();
    const dt = (now - t.lastAt) / 1000;
    if (dt >= 0.3) {
      const instant = (event.loaded - t.lastLoaded) / dt;
      t.speed = t.speed ? t.speed * 0.7 + instant * 0.3 : instant;
      t.lastAt = now;
      t.lastLoaded = event.loaded;
    }
    t.loaded = event.loaded;
    scheduleTransferRender();
  });
  xhr.addEventListener("load", () => {
    if (xhr.status === 201) {
      const saved = xhr.response?.files?.[0];
      t.state = "done";
      t.loaded = t.size;
      t.savedName = saved?.name || t.name;
      state.sentInBatch.push(t.savedName);
      state.justAdded.add(t.savedName);
      state.etag = null;
      setTimeout(() => removeTransfer(t), 3500);
      poll(true);
    } else {
      const err = xhr.response?.error;
      if (xhr.status === 401 && err?.code === "pin_required") showLock();
      t.state = "error";
      t.error = err?.message || `Upload failed (${xhr.status || "no response"}).`;
      t.fatal = [403, 413].includes(xhr.status) || err?.code === "invalid_name";
    }
    settle();
  });
  xhr.addEventListener("error", () => {
    t.state = "error";
    t.error = "Connection lost. Check your Wi-Fi and try again.";
    settle();
  });
  xhr.addEventListener("abort", () => {
    t.state = "canceled";
    removeTransfer(t);
    settle();
  });
  xhr.send(t.file);
}

function settle() {
  pump();
  renderTransfers();
  const list = [...state.transfers.values()];
  const busy = list.some((t) => t.state === "uploading" || t.state === "queued");
  if (busy) return;
  // Finished rows disappear after a few seconds, so count sends as they happen.
  const done = state.sentInBatch;
  state.sentInBatch = [];
  const failed = list.filter((t) => t.state === "error" && !t.announced);
  for (const t of failed) t.announced = true;
  if (done.length && !failed.length) {
    toast(done.length === 1 ? `Sent “${done[0]}”` : `Sent ${done.length} files`, { tone: "success", icon: "check" });
  } else if (failed.length) {
    toast(
      done.length ? `Sent ${done.length} of ${done.length + failed.length} files` : failed.length === 1 ? "Couldn’t send the file" : `Couldn’t send ${failed.length} files`,
      { tone: "error", icon: "alert" },
    );
  }
}

function cancelOrDismiss(t) {
  if (t.state === "uploading") t.xhr.abort();
  else {
    t.state = "canceled";
    removeTransfer(t);
    settle();
  }
}

function retryTransfer(t) {
  t.state = "queued";
  t.announced = false;
  pump();
  renderTransfers();
}

function removeTransfer(t) {
  if (!state.transfers.has(t.id)) return;
  state.transfers.delete(t.id);
  t.el.classList.add("is-leaving");
  setTimeout(() => {
    t.el.remove();
    if (!state.transfers.size) $("#transfers").hidden = true;
    renderTransfers();
  }, 260);
}

function buildTransferRow(t) {
  const file = { name: t.name, kind: guessKind(t.name) };
  const thumb = h("span", { class: "thumb", "aria-hidden": "true" }, icon(KIND_ICON[file.kind]));
  thumb.style.setProperty("--kind", `var(--k-${file.kind})`);
  const bar = h("span", { class: "progress-bar" });
  const progress = h("span", { class: "progress", role: "progressbar", "aria-label": `Sending ${t.name}`, "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": "0" }, bar);
  const row = h(
    "li",
    { class: "row transfer-row is-entering" },
    thumb,
    h("div", { class: "row-main" }, h("span", { class: "row-title", title: t.name }, t.name), h("span", { class: "row-sub" }), progress),
    h("div", { class: "row-actions" }),
  );
  row._parts = { bar, progress, sub: row.querySelector(".row-sub"), actions: row.querySelector(".row-actions") };
  return row;
}


function scheduleTransferRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    renderTransfers();
  });
}

function renderTransfers() {
  let sent = 0;
  let total = 0;
  let active = 0;
  for (const t of state.transfers.values()) {
    const { bar, progress, sub, actions } = t.el._parts;
    const pct = t.size ? Math.min(100, (t.loaded / t.size) * 100) : t.state === "done" ? 100 : 0;
    t.el.dataset.state = t.state;
    bar.style.transform = `scaleX(${pct / 100})`;
    progress.setAttribute("aria-valuenow", String(Math.round(pct)));
    if (t.state === "uploading" || t.state === "queued") {
      active += 1;
      sent += t.loaded;
      total += t.size;
    }
    let text;
    if (t.state === "queued") text = `Waiting · ${formatBytes(t.size)}`;
    else if (t.state === "uploading") {
      const eta = t.speed > 0 ? formatDuration((t.size - t.loaded) / t.speed) : "";
      text = [`${formatBytes(t.loaded)} of ${formatBytes(t.size)}`, t.speed > 0 && `${formatBytes(t.speed)}/s`, eta].filter(Boolean).join(" · ");
      if (t.loaded >= t.size && t.size > 0) text = "Finishing…";
    } else if (t.state === "done") text = t.savedName && t.savedName !== t.name ? `Sent as “${t.savedName}”` : "Sent";
    else if (t.state === "error") text = t.error;
    else text = "";
    if (sub.textContent !== text) sub.textContent = text;

    const mode = t.state === "done" ? "done" : t.state === "error" ? (t.fatal ? "error-fatal" : "error") : "active";
    if (actions.dataset.mode !== mode) {
      actions.dataset.mode = mode;
      actions.replaceChildren(
        ...(mode === "done"
          ? [h("span", { class: "status-badge", "aria-label": "Sent" }, icon("check"))]
          : [
              mode === "error" && h("button", { class: "icon-btn is-accent", type: "button", "aria-label": `Retry ${t.name}`, title: "Retry", onclick: () => retryTransfer(t) }, icon("retry")),
              h("button", { class: "icon-btn", type: "button", "aria-label": mode === "active" ? `Cancel ${t.name}` : `Dismiss ${t.name}`, title: mode === "active" ? "Cancel" : "Dismiss", onclick: () => cancelOrDismiss(t) }, icon("x")),
            ]
        ).filter(Boolean),
      );
    }
  }
  const failed = [...state.transfers.values()].filter((t) => t.state === "error").length;
  $("#clear-transfers").hidden = failed === 0 || active > 0;
  $("#transfers-meta").textContent = active
    ? `${plural(active, "file")} · ${Math.floor(total ? (sent / total) * 100 : 0)}%`
    : failed
      ? `${failed} failed`
      : "";
  document.title = active ? `${Math.floor(total ? (sent / total) * 100 : 0)}% · Open Transfer` : "Open Transfer";
}

window.addEventListener("beforeunload", (event) => {
  if ([...state.transfers.values()].some((t) => t.state === "uploading" || t.state === "queued")) {
    event.preventDefault();
    event.returnValue = "";
  }
});

// ---------------------------------------------------- drag, drop & paste

function hasFiles(event) {
  return [...(event.dataTransfer?.types || [])].includes("Files");
}

function droppedFiles(event) {
  const items = [...(event.dataTransfer?.items || [])];
  const folders = items.filter((item) => item.webkitGetAsEntry?.()?.isDirectory).length;
  if (folders) {
    toast(folders === 1 ? "Folders can’t be sent yet — zip it first." : "Folders can’t be sent yet — zip them first.", { tone: "error", icon: "alert" });
  }
  if (items.length) {
    return items.filter((item) => item.kind === "file" && !item.webkitGetAsEntry?.()?.isDirectory).map((item) => item.getAsFile()).filter(Boolean);
  }
  return [...(event.dataTransfer?.files || [])];
}

function setupDropAndPaste() {
  const overlay = $("#drop-overlay");
  const zone = $("#dropzone");
  const input = $("#file-input");
  let depth = 0;
  const canDrop = () => (can("upload") || can("send")) && !$("#main-view").hidden;
  const hide = () => {
    depth = 0;
    overlay.classList.remove("is-visible");
    zone.classList.remove("is-over");
    document.body.classList.remove("is-dragging");
  };
  document.addEventListener("ot-drop-handled", hide);

  window.addEventListener("dragenter", (event) => {
    if (!hasFiles(event) || !canDrop()) return;
    event.preventDefault();
    depth += 1;
    // With devices on screen, keep them visible so files can be dropped onto one.
    if (isDeviceMode()) document.body.classList.add("is-dragging");
    else overlay.classList.add("is-visible");
    zone.classList.add("is-over");
  });
  window.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = canDrop() ? "copy" : "none";
  });
  window.addEventListener("dragleave", (event) => {
    if (!hasFiles(event)) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) hide();
  });
  window.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    hide();
    if (canDrop()) enqueueFiles(droppedFiles(event), { source: "drop" });
  });

  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      input.click();
    }
  });
  input.addEventListener("change", () => {
    enqueueFiles(input.files);
    input.value = "";
  });

  document.addEventListener("paste", (event) => {
    if (event.target.closest?.("input, textarea") || !canDrop()) return;
    const files = [...(event.clipboardData?.files || [])];
    if (files.length) {
      event.preventDefault();
      enqueueFiles(files, { source: "paste" });
    }
  });
}

// --------------------------------------------------------------------- PIN

function setupPinForm() {
  const form = $("#pin-form");
  const input = $("#pin-input");
  const error = $("#pin-error");
  const submit = $("#pin-submit");
  let lockedUntil = 0;

  input.addEventListener("input", () => {
    input.classList.remove("is-invalid");
    error.textContent = "";
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const pin = input.value.trim();
    if (!pin || Date.now() < lockedUntil) return;
    submit.classList.add("is-busy");
    submit.disabled = true;
    try {
      await api("/api/auth", { method: "POST", json: { pin } });
      const { data } = await api("/api/info");
      applyInfo(data);
      input.value = "";
      enterMain();
    } catch (err) {
      input.classList.remove("is-invalid");
      void input.offsetWidth; // restart the shake animation
      input.classList.add("is-invalid");
      input.select();
      if (err.status === 429) {
        const wait = err.detail.retry_after || 30;
        lockedUntil = Date.now() + wait * 1000;
        const tick = () => {
          const left = Math.ceil((lockedUntil - Date.now()) / 1000);
          if (left > 0) {
            error.textContent = `Too many attempts. Try again in ${left} s.`;
            setTimeout(tick, 500);
          } else {
            error.textContent = "";
            submit.disabled = false;
          }
        };
        tick();
        submit.classList.remove("is-busy");
        return;
      }
      error.textContent = err.status === 0 ? err.message : err.message || "That PIN isn’t right.";
    }
    submit.classList.remove("is-busy");
    if (Date.now() >= lockedUntil) submit.disabled = false;
  });
}

// -------------------------------------------------------------------- boot

function boot() {
  setLockHandler(showLock);
  applyInfo(state.info);
  setupDropAndPaste();
  initNearby({
    requestPoll: () => poll(true),
    droppedFiles,
    libraryChanged: (inbox) => {
      if (inbox && inInbox()) {
        const sig = JSON.stringify(inbox.map((f) => [f.name, f.size, f.modified]));
        if (sig !== state.inboxSig) {
          state.inboxSig = sig;
          state.files = inbox;
          renderFiles();
        }
      } else if (!inbox) {
        state.etag = null;
      }
    },
  });
  for (const button of [$("#connect-button"), $("#empty-connect")]) button.addEventListener("click", () => openAddDevice());
  setupPinForm();
  $("#search").addEventListener("input", (event) => {
    state.search = event.target.value;
    renderFiles();
  });
  $("#clear-transfers").addEventListener("click", () => {
    for (const t of [...state.transfers.values()]) if (t.state === "error") removeTransfer(t);
  });
  $("#fatal-retry").addEventListener("click", () => {
    state.failures = 0;
    showView("main");
    poll(true);
  });
  if (state.info.auth.required && !state.info.auth.authenticated) showLock();
  else enterMain();
}

boot();
