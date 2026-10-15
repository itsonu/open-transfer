// Open Transfer — shared helpers: DOM, formatting, API, toasts.

export const $ = (selector, root = document) => root.querySelector(selector);

export const state = {
  info: JSON.parse(document.getElementById("boot").textContent),
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

export async function api(path, { method = "GET", json, headers = {} } = {}) {
  const init = { method, credentials: "same-origin", headers: { Accept: "application/json", ...headers } };
  if (json !== undefined) {
    init.body = JSON.stringify(json);
    init.headers["Content-Type"] = "application/json";
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(0, "network", "Can’t reach the sharing computer.");
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
  let timer;
  const close = () => {
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
