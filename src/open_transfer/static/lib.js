// Open Transfer — shared helpers: DOM, formatting, API, toasts.

export const $ = (selector, root = document) => root.querySelector(selector);

function readBoot() {
  try {
    return JSON.parse(document.getElementById("boot").textContent);
  } catch {
    return { app: "Open Transfer", version: "", device: "", owner: false, auth: { required: false, authenticated: true } };
  }
}

export const state = {
  info: readBoot(),
};

export const can = (perm) => Boolean(state.info.permissions?.[perm]);

let onLocked = () => {};
export function setLockHandler(fn) {
  onLocked = fn;
}

// ---------------------------------------------------------------- DOM

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value == null) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key === "style") Object.assign(el.style, value); // CSSOM, allowed by the CSP
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  el.append(...children.flat().filter((c) => c != null && c !== false));
  return el;
}

export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

export function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1000) return `${bytes} ${bytes === 1 ? "byte" : "bytes"}`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = "B";
  for (const u of units) {
    value /= 1000;
    unit = u;
    if (value < 1000) break;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${unit}`;
}

export function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} s left`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min left`;
  return `${(seconds / 3600).toFixed(1)} hr left`;
}

export function relativeTime(epochSeconds) {
  const diff = Date.now() / 1000 - epochSeconds;
  if (diff < 45) return "Just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} hr ago`;
  const date = new Date(epochSeconds * 1000);
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: sameYear ? undefined : "numeric" });
}

export function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

export function guessKind(name) {
  const ext = (name.split(".").pop() || "").toLowerCase();
  const map = {
    image: "png jpg jpeg gif webp heic heif bmp tiff svg avif",
    video: "mp4 mov m4v webm mkv avi",
    audio: "mp3 m4a aac wav flac ogg opus",
    archive: "zip rar 7z tar gz tgz bz2 xz dmg iso",
    document: "pdf doc docx odt rtf txt md pages epub",
    spreadsheet: "xls xlsx csv ods numbers",
    presentation: "ppt pptx odp key",
    code: "py js ts json html css sh c cpp go rs java",
    app: "apk exe msi pkg deb rpm appimage",
  };
  return Object.keys(map).find((k) => map[k].split(" ").includes(ext)) || "other";
}

// -------------------------------------------------------------------- API

export class ApiError extends Error {
  constructor(status, code, message, detail = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

const API_TIMEOUT_MS = 20000;

// ------------------------------------------------------- chunked uploads
//
// Files bigger than CHUNK_THRESHOLD are split into CHUNK_SIZE pieces (see
// chunks.py on the server). One lost connection loses one chunk, not the
// whole file — each chunk is retried and the upload resumes where it left off.

export const chunkLimits = () => ({
  size: state.info.limits?.chunk_size || 2 * 1024 * 1024,
  threshold: state.info.limits?.chunk_threshold || 4 * 1024 * 1024,
});

export const shouldChunk = (bytes) => bytes > chunkLimits().threshold;

export function newUploadId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID().replace(/-/g, "").slice(0, 22);
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** PUT one chunk with ``Content-Range``. Resolves ``{received, total, done, data}``. */
export async function putChunk(url, { method = "PUT", chunk, start, end, total, uploadId, name, signal, timeout = 60000 }) {
  const headers = {
    "Content-Range": `bytes ${start}-${end}/${total}`,
    "X-Upload-Id": uploadId,
    "Content-Type": "application/octet-stream",
  };
  if (name) headers["X-Filename"] = encodeURIComponent(name);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  if (signal) signal.addEventListener("abort", () => ctrl.abort(), { once: true });
  let res;
  try {
    res = await fetch(url, { method, credentials: "same-origin", headers, body: chunk, signal: ctrl.signal });
  } catch (err) {
    if (err?.name === "AbortError") throw new ApiError(0, "timeout", "The connection timed out sending that chunk.");
    throw new ApiError(0, "network", "Can’t reach the sharing computer.");
  } finally {
    clearTimeout(timer);
  }
  // 308 = "Resume Incomplete": the chunk landed, more to come.
  if (res.status === 308 || res.status === 200 || res.status === 201) {
    const data = (res.headers.get("Content-Type") || "").includes("json") ? await res.json().catch(() => null) : null;
    const received = data?.received ?? end + 1;
    return { received, total, done: res.status === 201 || data?.done === true, data };
  }
  const body = (res.headers.get("Content-Type") || "").includes("json") ? await res.json().catch(() => null) : null;
  const err = body?.error || {};
  throw new ApiError(res.status, err.code || `http_${res.status}`, err.message || `Sending failed (${res.status}).`, err);
}

/** Send one chunk with retries (3 tries, backoff). */
export async function putChunkRetry(url, opts, tries = 3) {
  let last;
  for (let attempt = 0; attempt < tries; attempt += 1) {
    try {
      return await putChunk(url, opts);
    } catch (err) {
      last = err;
      // Don't retry auth / validation errors — only network + 5xx + 408/429.
      if (err.status && err.status !== 0 && err.status < 500 && ![408, 429].includes(err.status)) throw err;
      if (opts.signal?.aborted) throw err;
      await sleep(400 * 2 ** attempt);
    }
  }
  throw last;
}

/** Ask the server how many bytes of ``uploadId`` already arrived (for resume). */
export async function fetchChunkStatus(statusUrl, signal) {
  try {
    const { data } = await api(statusUrl, { signal, timeout: 8000 });
    return data?.received || 0;
  } catch {
    return 0;
  }
}

/** Full chunk state: bytes received plus the covered ranges (for parallel resume). */
export async function fetchChunkState(statusUrl, signal) {
  try {
    const { data } = await api(statusUrl, { signal, timeout: 8000 });
    return { received: data?.received || 0, total: data?.total || 0, ranges: data?.ranges || [], done: data?.done === true, data };
  } catch {
    return { received: 0, total: 0, ranges: [] };
  }
}

/** Chunk-aligned ``{start, end}`` spans (end exclusive) not yet covered by ``ranges``. */
export function missingChunks(ranges, total, chunkSize) {
  const covered = [...(ranges || [])]
    .map(([s, e]) => [Number(s), Number(e)])
    .filter(([s, e]) => Number.isFinite(s) && Number.isFinite(e) && e > s)
    .sort((a, b) => a[0] - b[0]);
  const out = [];
  let offset = 0;
  while (offset < total) {
    const end = Math.min(offset + chunkSize, total);
    const done = covered.some(([s, e]) => s <= offset && e >= end);
    if (!done) out.push({ start: offset, end });
    offset = end;
  }
  return out;
}

/**
 * Upload a file in parallel chunks (up to ``concurrency`` in flight).
 * Resolves with the final chunk's ``data``. ``onProgress(sent, total)`` fires
 * after every landed chunk. Aborting ``signal`` pauses: already-stored chunks
 * stay on the server, so calling again resumes where it stopped.
 */
export async function uploadParallel(
  url,
  { method = "PUT", file, total = file.size, name, uploadId, statusUrl, signal, timeout, concurrency = 3, onProgress },
) {
  const { size: chunkSize } = chunkLimits();
  const state0 = statusUrl ? await fetchChunkState(statusUrl, signal) : { received: 0, ranges: [] };
  if (state0.done && total > 0) {
    // Already finished (e.g. paused just as the last pieces landed): nothing to resend.
    onProgress?.(total, total);
    return { data: state0.data };
  }
  let queue = missingChunks(state0.ranges, total, chunkSize);
  if (!queue.length && total > 0) {
    // Every piece is there but the server never confirmed the end: send the last
    // one again so it finishes, instead of the whole file.
    queue = missingChunks([], total, chunkSize).slice(-1);
  }
  let sent = total - queue.reduce((n, c) => n + (c.end - c.start), 0);
  onProgress?.(sent, total);
  let finalData = null;
  let failed = null;
  let cursor = 0;
  const workers = Array.from({ length: Math.min(concurrency, queue.length) }, async () => {
    while (cursor < queue.length) {
      if (signal?.aborted) throw new DOMException("paused", "AbortError");
      const span = queue[cursor];
      cursor += 1;
      const blob = file.slice(span.start, span.end);
      const { data } = await putChunkRetry(
        url,
        {
          method, chunk: blob, start: span.start, end: span.end - 1, total,
          uploadId, name, signal, timeout,
        },
      );
      finalData = data;
      sent += span.end - span.start;
      onProgress?.(Math.min(sent, total), total);
    }
  });
  try {
    await Promise.all(workers);
  } catch (err) {
    failed = err;
  }
  if (signal?.aborted) {
    const abort = new ApiError(0, "paused", "Paused — resume to continue where it stopped.");
    abort.paused = true;
    throw abort;
  }
  if (failed) throw failed;
  // The last chunk's response carries the result (saved file or fan-out report).
  if (!finalData && total === 0) return { data: null };
  if (!finalData) {
    const check = statusUrl ? await fetchChunkState(statusUrl, signal) : null;
    if (!check || check.received < total) throw new ApiError(0, "network", "Some chunks didn’t arrive. Try again to resume.");
  }
  return { data: finalData };
}

// ------------------------------------------------------- transfer history
//
// Kept in this browser only (localStorage): the last 30 finished transfers
// with their average speed, so repeat sends are one glance away.

const HISTORY_KEY = "ot-history";
const HISTORY_MAX = 30;

export function loadHistory() {
  try {
    const saved = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(saved) ? saved.filter((e) => e && typeof e.name === "string") : [];
  } catch {
    return [];
  }
}

export function addHistory(entry) {
  try {
    const list = loadHistory();
    list.unshift({ when: Date.now(), ...entry });
    localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, HISTORY_MAX)));
  } catch {
    /* storage blocked: history is best effort */
  }
  return loadHistory();
}

export function clearHistory() {
  try {
    localStorage.removeItem(HISTORY_KEY);
  } catch {
    /* ignore */
  }
}

// ------------------------------------------------------------- speed graph
//
// Each upload samples its speed; the samples render as a tiny inline SVG
// sparkline (no canvas, no dependencies) in the transfer row and in history.

export function pushSample(samples, speed) {
  samples.push(speed);
  if (samples.length > 40) samples.shift();
  return samples;
}

export function sparklineSVG(samples, width = 64, height = 20) {
  const points = (samples || []).filter((v) => Number.isFinite(v) && v >= 0).slice(-40);
  if (points.length < 2) return null;
  const max = Math.max(...points, 1);
  const step = width / (points.length - 1);
  const path = points.map((v, i) => `${(i * step).toFixed(1)},${(height - (v / max) * (height - 2) - 1).toFixed(1)}`).join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(height));
  svg.setAttribute("aria-hidden", "true");
  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  line.setAttribute("points", path);
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "currentColor");
  line.setAttribute("stroke-width", "1.5");
  line.setAttribute("stroke-linecap", "round");
  line.setAttribute("stroke-linejoin", "round");
  svg.append(line);
  return svg;
}

export async function api(path, { method = "GET", json, headers = {}, timeout = API_TIMEOUT_MS, signal } = {}) {
  const init = { method, credentials: "same-origin", headers: { Accept: "application/json", ...headers } };
  if (json !== undefined) {
    init.body = JSON.stringify(json);
    init.headers["Content-Type"] = "application/json";
  }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  if (signal) signal.addEventListener("abort", () => ctrl.abort(), { once: true });
  init.signal = ctrl.signal;
  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    if (err?.name === "AbortError") throw new ApiError(0, "timeout", "The sharing computer is taking too long to answer.");
    throw new ApiError(0, "network", "Can’t reach the sharing computer.");
  } finally {
    clearTimeout(timer);
  }
  if (res.status === 304) return { notModified: true, res };
  const body = (res.headers.get("Content-Type") || "").includes("json") ? await res.json().catch(() => null) : null;
  if (!res.ok) {
    const err = body?.error || {};
    if (res.status === 401 && err.code === "pin_required") onLocked();
    throw new ApiError(res.status, err.code || `http_${res.status}`, err.message || `Request failed (${res.status}).`, err);
  }
  return { data: body, res };
}

// ------------------------------------------------------------------ toasts

export function toast(message, { tone = "info", icon: iconName, action, duration = 3200 } = {}) {
  const host = $("#toasts");
  while (host.children.length >= 3) host.firstElementChild.remove();
  let timer = 0;
  let gone = false;
  const close = () => {
    if (gone) return;
    gone = true;
    clearTimeout(timer);
    el.classList.add("is-leaving");
    setTimeout(() => el.remove(), 260);
  };
  const el = h(
    "div",
    { class: "toast", role: tone === "error" ? "alert" : "status", dataset: { tone } },
    iconName && icon(iconName),
    h("span", { class: "toast-text" }, message),
    action &&
      h(
        "button",
        {
          class: "toast-action",
          type: "button",
          onclick: () => {
            close();
            action.run();
          },
        },
        action.label,
      ),
  );
  host.append(el);
  const arm = () => {
    timer = setTimeout(close, duration);
  };
  el.addEventListener("mouseenter", () => clearTimeout(timer));
  el.addEventListener("mouseleave", arm);
  el.addEventListener("focusin", () => clearTimeout(timer));
  arm();
  return close;
}

// --------------------------------------------------------------- clipboard

export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the legacy path */
  }
  const area = h("textarea", { readonly: true, "aria-hidden": "true" });
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.append(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  area.remove();
  return ok;
}

// ------------------------------------------------------- sensitive text
// Mirrors open_transfer/sensitive.py (tests check both give the same answers):
// asks before private-looking text is sent, and it's never kept in history.

const SENSITIVE_KEY = /-----BEGIN [A-Z ]*PRIVATE KEY-----/;
const SENSITIVE_TOKEN = new RegExp(
  [
    "gh[pousr]_[A-Za-z0-9]{30,}",
    "github_pat_[A-Za-z0-9_]{30,}",
    "(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}",
    "(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}",
    "AKIA[0-9A-Z]{16}",
    "xox[abposr]-[A-Za-z0-9-]{10,}",
    "AIza[0-9A-Za-z_-]{35}",
    "eyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}",
  ].join("|"),
);
const SENSITIVE_LABEL = /\b(?:password|passwd|pwd|passcode|pin|secret|api[ _-]?key|token)\s*[:=]\s*\S+/i;

export const SENSITIVE_REASONS = {
  card: "a card number",
  code: "a one-time code",
  key: "a private key",
  token: "an access token or API key",
  password: "a password",
};

function luhn(digits) {
  let total = 0;
  [...digits].reverse().forEach((ch, i) => {
    let n = Number(ch);
    if (i % 2) n = n > 4 ? n * 2 - 9 : n * 2;
    total += n;
  });
  return total % 10 === 0;
}

function passwordLike(word) {
  const length = [...word].length;
  if (length < 8 || length > 64 || /\s/.test(word) || word.includes("://")) return false;
  const symbol = /[^\p{L}\p{N}]/u.test(word);
  const kinds = [/\p{Ll}/u.test(word), /\p{Lu}/u.test(word), /\p{Nd}/u.test(word), symbol].filter(Boolean).length;
  return kinds >= 4 || (kinds === 3 && symbol && length >= 10);
}

/** Why ``text`` looks private ("card", "code", "key", "token", "password"), or null. */
export function looksSensitive(text) {
  const value = String(text || "").trim();
  if (!value) return null;
  if (SENSITIVE_KEY.test(value)) return "key";
  if (value.length > 20000) return null;
  if (SENSITIVE_TOKEN.test(value)) return "token";
  if (SENSITIVE_LABEL.test(value)) return "password";
  if (/^(?:\d[ -]?){12,18}\d$/.test(value)) {
    const digits = value.replace(/\D/g, "");
    return digits.length >= 13 && digits.length <= 19 && luhn(digits) ? "card" : null;
  }
  if (/^\d{4,8}$/.test(value) && !(value.length === 4 && /^(19|20)/.test(value))) return "code";
  if (passwordLike(value)) return "password";
  return null;
}

/** A readable, sortable stamp for file names: 2026-10-22 07.31.05 */
export function stamp(date = new Date()) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}.${pad(date.getMinutes())}.${pad(date.getSeconds())}`;
}
