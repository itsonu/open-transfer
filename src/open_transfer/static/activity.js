// Open Transfer — activity: what was sent and received, newest first, grouped
// by day, with search and filters. This device's own window reads the history
// the app keeps (GET /api/history); a visitor's browser keeps its own list.

import { $, api, clearHistory, formatBytes, guessKind, h, icon, loadHistory, plural, relativeTime, sparklineSVG, state } from "./lib.js";

const KIND_ICON = {
  image: "image", video: "video", audio: "audio", archive: "archive", document: "doc",
  spreadsheet: "table", presentation: "slides", code: "code", app: "app", other: "file",
};
const STATUS = { failed: "Failed", declined: "Declined", canceled: "Canceled", expired: "No answer", partial: "Partly sent", not_sent: "Not sent" };
const THUMBS_KEY = "ot-thumbs";
const THUMBS_MAX = 40;

const activity = { entries: [], devices: new Map(), latest: null, timer: 0, generation: 0 };
const owner = () => Boolean(state.info.owner);

// ------------------------------------------------------- picture thumbnails
// The app's history has no pictures in it; thumbnails of what this window
// sent are kept here by transfer id.

function loadThumbs() {
  try {
    const saved = JSON.parse(localStorage.getItem(THUMBS_KEY) || "{}");
    return saved && typeof saved === "object" ? saved : {};
  } catch {
    return {};
  }
}

export function saveThumb(ref, thumb) {
  if (!ref || typeof thumb !== "string" || !thumb.startsWith("data:image/")) return;
  try {
    const thumbs = loadThumbs();
    thumbs[ref] = thumb;
    const keys = Object.keys(thumbs);
    for (const key of keys.slice(0, Math.max(0, keys.length - THUMBS_MAX))) delete thumbs[key];
    localStorage.setItem(THUMBS_KEY, JSON.stringify(thumbs));
  } catch {
    /* storage blocked: no thumbnail */
  }
}

// ------------------------------------------------------------------ data

function filters() {
  return {
    q: $("#history-search").value.trim(),
    device: $("#history-device").value,
    direction: $("#history-direction").value,
  };
}

/** A visitor's own list, in the same shape as the app's history. */
function localEntries() {
  return loadHistory().map((e, i) => ({
    id: `local-${i}`,
    at: (e.when || Date.now()) / 1000,
    kind: "transfer",
    direction: e.direction === "up" || e.direction === "send" ? "sent" : "received",
    title: e.name,
    size: e.size,
    status: e.status || "done",
    speed: e.speed || 0,
    samples: e.samples,
    thumb: e.thumb,
    devices: [],
  }));
}

export async function refreshActivity() {
  const generation = ++activity.generation;
  const f = filters();
  let entries;
  let total;
  if (owner()) {
    const query = new URLSearchParams(Object.entries(f).filter(([, v]) => v)).toString();
    try {
      const { data } = await api(`/api/history${query ? `?${query}` : ""}`);
      entries = data.entries;
      activity.latest = data.latest;
      total = data.latest ? Infinity : 0; // anything at all, before filtering
    } catch {
      return; // the next change tries again
    }
  } else {
    const all = localEntries();
    total = all.length;
    const needle = f.q.toLowerCase();
    entries = all.filter((e) => (!f.direction || e.direction === f.direction) && (!needle || e.title.toLowerCase().includes(needle)));
  }
  if (generation !== activity.generation) return; // a newer refresh is on its way
  activity.entries = entries;
  for (const e of entries) for (const d of e.devices || []) if (d.id && d.name) activity.devices.set(d.id, d.name);
  render(total);
}

// ---------------------------------------------------------------- render

function dayLabel(epochSeconds) {
  const date = new Date(epochSeconds * 1000);
  const today = new Date();
  const start = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((start(today) - start(date)) / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return date.toLocaleDateString(undefined, { weekday: days < 7 ? "long" : undefined, day: "numeric", month: "long", year: date.getFullYear() === today.getFullYear() ? undefined : "numeric" });
}

function who(e) {
  const names = (e.devices || []).map((d) => d.name).filter(Boolean);
  if (!names.length) return "";
  const list = names.length > 2 ? `${names.slice(0, 2).join(", ")} and ${names.length - 2} more` : names.join(" and ");
  return e.direction === "received" ? `From ${list}` : `To ${list}`;
}

function thumbFor(e, thumbs) {
  if (typeof e.thumb === "string" && e.thumb.startsWith("data:image/")) return e.thumb;
  if (e.ref && thumbs[e.ref]) return thumbs[e.ref];
  if (owner() && e.image) return `/files/${encodeURIComponent(e.image)}?inline=1`;
  return null;
}

function row(e, thumbs) {
  const clip = e.kind === "clipboard";
  const kind = clip ? "copy" : KIND_ICON[e.title?.endsWith(".zip") ? "archive" : guessKind(e.title || "")] || "file";
  const thumb = h("span", { class: "thumb", "aria-hidden": "true" }, icon(kind));
  const picture = thumbFor(e, thumbs);
  if (picture) {
    const img = h("img", { src: picture, alt: "", loading: "lazy" });
    img.addEventListener("load", () => img.classList.add("is-loaded"), { once: true });
    img.addEventListener("error", () => img.remove(), { once: true });
    if (picture.startsWith("data:")) img.classList.add("is-loaded");
    thumb.append(img);
  }
  const speed = e.speed >= 1000 ? `${formatBytes(e.speed)}/s` : "";
  const sub = [formatBytes(e.size), relativeTime(e.at), speed, STATUS[e.status]].filter(Boolean).join(" · ");
  const title = clip ? `Clipboard · ${e.title}` : e.title;
  const sent = e.direction === "sent";
  const line = h("div", { class: "history-head" }, h("span", { class: "row-title", title }, title), who(e) && h("span", { class: "history-who" }, who(e)));
  const main = h("div", { class: "row-main" }, line, h("span", { class: "row-sub" }, sub));
  const graph = e.samples?.length >= 2 ? sparklineSVG(e.samples) : null;
  if (graph) main.append(h("span", { class: "spark-wrap", title: "Speed over time" }, graph));
  return h(
    "li",
    { class: "row history-row", dataset: { status: e.status, kind: e.kind } },
    thumb,
    main,
    h("span", { class: "history-dir", dataset: { dir: sent ? "up" : "down" } }, sent ? "Sent" : "Received"),
  );
}

function render(total) {
  const section = $("#history-section");
  const list = $("#history-list");
  const f = filters();
  const narrowed = Boolean(f.q || f.device || f.direction);
  section.hidden = total === 0 && !narrowed;
  if (section.hidden) return;
  $("#history-filters").hidden = false;
  const devices = $("#history-device");
  devices.hidden = !owner();
  devices.closest("label").hidden = !owner();
  const options = [...activity.devices].sort((a, b) => a[1].localeCompare(b[1]));
  const sig = JSON.stringify(options);
  if (devices.dataset.sig !== sig) {
    const chosen = devices.value;
    devices.dataset.sig = sig;
    devices.replaceChildren(h("option", { value: "" }, "All devices"), ...options.map(([id, name]) => h("option", { value: id }, name)));
    devices.value = options.some(([id]) => id === chosen) ? chosen : "";
  }
  $("#history-meta").textContent = activity.entries.length ? plural(activity.entries.length, narrowed ? "match" : "item") : "";
  $("#history-empty").hidden = activity.entries.length > 0;
  list.hidden = activity.entries.length === 0;
  const thumbs = loadThumbs();
  const items = [];
  let day = "";
  for (const e of activity.entries.slice(0, 100)) {
    const label = dayLabel(e.at);
    if (label !== day) {
      day = label;
      items.push(h("li", { class: "history-day", role: "presentation" }, label));
    }
    items.push(row(e, thumbs));
  }
  list.replaceChildren(...items);
}

// ---------------------------------------------------------------- setup

export function initActivity() {
  const later = () => {
    clearTimeout(activity.timer);
    activity.timer = setTimeout(refreshActivity, 200);
  };
  $("#history-search").addEventListener("input", later);
  $("#history-device").addEventListener("change", refreshActivity);
  $("#history-direction").addEventListener("change", refreshActivity);
  $("#clear-history").addEventListener("click", async () => {
    if (owner()) await api("/api/history", { method: "DELETE" }).catch(() => {});
    clearHistory();
    activity.devices.clear();
    refreshActivity();
  });
  window.addEventListener("ot-history", refreshActivity);
  window.addEventListener("ot-mesh-state", (event) => {
    const latest = event.detail.history?.latest;
    if (latest !== undefined && latest !== activity.latest) {
      activity.latest = latest;
      refreshActivity();
    }
  });
  refreshActivity();
}
