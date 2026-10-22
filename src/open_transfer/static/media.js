// Open Transfer — media: the preview dialog, image thumbnails, and each
// file's quick-actions menu. No build step; used by app.js and nearby.js.

import { $, formatBytes, h, icon, toast } from "./lib.js";

const IMAGE_MIME = new Set(["image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp", "image/svg+xml"]);
const VIDEO_MIME = new Set(["video/mp4", "video/webm", "video/ogg", "video/quicktime"]);
const TEXT_EXT = new Set("txt md markdown csv tsv log json xml yaml yml ini cfg conf toml py js mjs ts tsx jsx html htm css scss sh bash zsh c h cpp hpp go rs java kt swift rb php sql srt vtt".split(" "));
const TEXT_PREVIEW_BYTES = 512 * 1024;
const THUMB_SIZE = 96;

const ext = (name) => (name.includes(".") ? name.split(".").pop().toLowerCase() : "");

/** What the preview can show for ``file``: image, video, audio, pdf, text — or null. */
export function previewKind(file) {
  const mime = file.mime || "";
  if (IMAGE_MIME.has(mime)) return "image";
  if (VIDEO_MIME.has(mime)) return "video";
  if (mime.startsWith("audio/")) return "audio";
  if (mime === "application/pdf") return "pdf";
  if (mime.startsWith("text/") || mime === "application/json" || mime === "application/xml" || TEXT_EXT.has(ext(file.name))) return "text";
  return null;
}

export const isVideo = (file) => VIDEO_MIME.has(file.mime || "");
export const isImage = (file) => IMAGE_MIME.has(file.mime || file.type || "");

// ------------------------------------------------------------ thumbnails

/** A small JPEG data: URL of an image File or same-origin URL, or null. */
export async function makeThumb(source) {
  try {
    const img = new Image();
    const url = typeof source === "string" ? source : URL.createObjectURL(source);
    img.decoding = "async";
    img.src = url;
    try {
      await img.decode();
    } finally {
      if (typeof source !== "string") URL.revokeObjectURL(url);
    }
    const scale = THUMB_SIZE / Math.min(img.naturalWidth, img.naturalHeight);
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = THUMB_SIZE;
    const w = img.naturalWidth * scale;
    const hgt = img.naturalHeight * scale;
    canvas.getContext("2d").drawImage(img, (THUMB_SIZE - w) / 2, (THUMB_SIZE - hgt) / 2, w, hgt);
    return canvas.toDataURL("image/jpeg", 0.72);
  } catch {
    return null; // not an image this browser can decode
  }
}

/** A video's first frame as its thumbnail (only the metadata is fetched). */
export function videoThumb(url) {
  const video = h("video", { src: `${url}#t=0.1`, preload: "metadata", muted: true, playsinline: true, tabindex: "-1", "aria-hidden": "true" });
  video.muted = true;
  video.addEventListener("loadeddata", () => video.classList.add("is-loaded"), { once: true });
  video.addEventListener("error", () => video.classList.add("is-broken"), { once: true });
  return video;
}

// -------------------------------------------------------------- preview

const preview = { files: [], index: 0, urlFor: null, trigger: null, controller: null };

/**
 * Show ``files[index]`` full screen. ``urlFor(file, inline)`` gives its URL;
 * ← and → step through ``files`` (wrapping), Esc closes.
 */
export function openPreview(files, index, { urlFor, trigger = document.activeElement } = {}) {
  if (!files.length) return;
  Object.assign(preview, { files, index, urlFor, trigger });
  const dialog = $("#preview-dialog");
  showPreview();
  if (!dialog.open) dialog.showModal();
  requestAnimationFrame(() => $("#preview-close").focus());
}

function step(delta) {
  const n = preview.files.length;
  preview.index = (preview.index + delta + n) % n;
  showPreview();
}

function showPreview() {
  preview.controller?.abort();
  preview.controller = new AbortController();
  const file = preview.files[preview.index];
  const kind = previewKind(file);
  const url = preview.urlFor(file, true);
  $("#preview-title").textContent = file.name;
  $("#preview-title").title = file.name;
  $("#preview-count").textContent = preview.files.length > 1 ? `${preview.index + 1} of ${preview.files.length}` : "";
  $("#preview-size").textContent = formatBytes(file.size);
  const save = $("#preview-save");
  save.href = preview.urlFor(file, false);
  save.setAttribute("download", file.name);
  save.setAttribute("aria-label", `Save ${file.name}`);
  for (const id of ["#preview-prev", "#preview-next"]) $(id).hidden = preview.files.length < 2;
  const stage = $("#preview-stage");
  stage.dataset.kind = kind || "none";
  if (kind === "image") stage.replaceChildren(h("img", { src: url, alt: file.name }));
  else if (kind === "video") stage.replaceChildren(h("video", { src: url, controls: true, playsinline: true, preload: "metadata" }));
  else if (kind === "audio") stage.replaceChildren(h("div", { class: "preview-audio" }, icon("audio"), h("audio", { src: url, controls: true, preload: "metadata" })));
  else if (kind === "pdf") stage.replaceChildren(h("iframe", { src: url, title: file.name }));
  else if (kind === "text") showText(stage, file, preview.controller.signal);
  else {
    stage.replaceChildren(
      h("div", { class: "preview-none" }, icon("file"), h("p", {}, "No preview for this kind of file."), h("a", { class: "btn btn-tinted", href: save.href, download: file.name }, icon("download"), h("span", {}, "Save"))),
    );
  }
}

async function showText(stage, file, signal) {
  const pre = h("pre", { class: "preview-text", tabindex: "0" });
  stage.replaceChildren(pre);
  pre.textContent = "Loading…";
  try {
    const res = await fetch(preview.urlFor(file, false), { signal, headers: { Range: `bytes=0-${TEXT_PREVIEW_BYTES - 1}` } });
    if (!res.ok) throw new Error(String(res.status));
    const text = await res.text();
    pre.textContent = text; // always as text: never parsed as HTML
    if (file.size > TEXT_PREVIEW_BYTES) stage.append(h("p", { class: "muted preview-note" }, `Showing the first ${formatBytes(TEXT_PREVIEW_BYTES)} of ${formatBytes(file.size)}.`));
  } catch (err) {
    if (err.name !== "AbortError") pre.textContent = "Couldn’t load this file.";
  }
}

function closePreview() {
  const dialog = $("#preview-dialog");
  if (dialog.open) dialog.close();
}

function setupPreview() {
  const dialog = $("#preview-dialog");
  $("#preview-close").addEventListener("click", closePreview);
  $("#preview-prev").addEventListener("click", () => step(-1));
  $("#preview-next").addEventListener("click", () => step(1));
  dialog.addEventListener("keydown", (event) => {
    if (event.target.closest?.("video, audio")) return; // their own arrow keys seek
    if (event.key === "ArrowRight") step(1);
    else if (event.key === "ArrowLeft") step(-1);
    else return;
    event.preventDefault();
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog || event.target.id === "preview-stage") closePreview();
  });
  dialog.addEventListener("close", () => {
    preview.controller?.abort();
    $("#preview-stage").replaceChildren(); // stop any video or audio
    if (preview.trigger?.isConnected) preview.trigger.focus();
  });
}

// ------------------------------------------------------------ quick actions

const menu = { opener: null, scrollY: 0 };

/**
 * Open the actions menu for a file. ``items`` are ``{ label, icon, run, danger }``;
 * ``at`` is the element it belongs to, or ``{ x, y }`` for a right-click.
 */
export function openMenu(items, { at, opener = at, label = "File actions" } = {}) {
  const el = $("#file-menu");
  menu.opener = opener instanceof Element ? opener : null;
  el.setAttribute("aria-label", label);
  el.replaceChildren(
    ...items.map((item) =>
      h(
        "button",
        {
          class: `menu-item${item.danger ? " is-danger" : ""}`,
          type: "button",
          role: "menuitem",
          tabindex: "-1",
          onclick: () => {
            closeMenu(false);
            item.run();
          },
        },
        icon(item.icon),
        h("span", {}, item.label),
      ),
    ),
  );
  el.hidden = false;
  const box = at instanceof Element ? at.getBoundingClientRect() : { left: at.x, right: at.x, top: at.y, bottom: at.y };
  const width = el.offsetWidth;
  const height = el.offsetHeight;
  const left = Math.min(Math.max(8, (at instanceof Element ? box.right - width : box.left)), innerWidth - width - 8);
  const below = box.bottom + 4;
  const top = below + height > innerHeight - 8 ? Math.max(8, box.top - height - 4) : below;
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
  if (menu.opener) menu.opener.setAttribute("aria-expanded", "true");
  menu.scrollY = scrollY;
  el.querySelector("[role=menuitem]")?.focus();
}

function closeMenu(restoreFocus = true) {
  const el = $("#file-menu");
  if (el.hidden) return;
  el.hidden = true;
  if (menu.opener) {
    menu.opener.setAttribute("aria-expanded", "false");
    if (restoreFocus && menu.opener.isConnected) menu.opener.focus();
  }
  menu.opener = null;
}

function setupMenu() {
  const el = $("#file-menu");
  el.addEventListener("keydown", (event) => {
    const items = [...el.querySelectorAll("[role=menuitem]")];
    const i = items.indexOf(document.activeElement);
    const moves = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: items.length - 1 };
    if (event.key in moves) {
      event.preventDefault();
      items[(moves[event.key] + items.length) % items.length]?.focus();
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeMenu();
    } else if (event.key === "Tab") {
      closeMenu();
    }
  });
  document.addEventListener("pointerdown", (event) => {
    if (!el.hidden && !el.contains(event.target)) closeMenu(false);
  });
  window.addEventListener("resize", () => closeMenu(false));
  // Close when the page really scrolls away (not on a late event from the scroll that led here).
  document.addEventListener("scroll", () => Math.abs(scrollY - menu.scrollY) > 40 && closeMenu(false), { passive: true });
}

// --------------------------------------------------------------- clipboard

/** Copy an image to the clipboard (as PNG, the one type every browser accepts). */
export async function copyImage(url) {
  if (!navigator.clipboard?.write || !window.ClipboardItem) return false;
  const png = (async () => {
    const res = await fetch(url);
    const blob = await res.blob();
    if (blob.type === "image/png") return blob;
    const bitmap = await createImageBitmap(blob);
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    canvas.getContext("2d").drawImage(bitmap, 0, 0);
    return new Promise((resolve, reject) => canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("no image"))), "image/png"));
  })();
  try {
    await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
    return true;
  } catch {
    return false;
  }
}

/** Share a file through the system share sheet, where the browser can. */
export async function shareFile(url, file) {
  try {
    const blob = await (await fetch(url)).blob();
    const shared = new File([blob], file.name, { type: blob.type || file.mime });
    if (!navigator.canShare?.({ files: [shared] })) throw new Error("unsupported");
    await navigator.share({ files: [shared], title: file.name });
  } catch (err) {
    if (err.name !== "AbortError") toast("This browser can’t share files. Save it, then share it.", { tone: "error", icon: "alert" });
  }
}

export const canShareFiles = () => Boolean(navigator.share && navigator.canShare);

export function initMedia() {
  setupPreview();
  setupMenu();
}
