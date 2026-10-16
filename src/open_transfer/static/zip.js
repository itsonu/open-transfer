// Open Transfer — folder sending: zip-on-the-fly in the browser.
//
// Dragging a folder (or picking one with "Add folder") collects every file
// with its relative path, then packs them into a single stored (uncompressed)
// ZIP — no dependency, no worker, works offline. The ZIP goes through the
// normal pipeline, so chunking, resume and progress apply automatically.
//
// Stored (method 0) is deliberate: LANs are fast, compression would only burn
// phone batteries for little gain on photos/videos (already compressed).

const encoder = new TextEncoder();

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c;
  }
  return table;
})();

export function crc32(bytes) {
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function dosTime(date = new Date()) {
  return {
    time: ((date.getHours() << 11) | (date.getMinutes() << 5) | (date.getSeconds() >> 1)) & 0xffff,
    date: ((((date.getFullYear() - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate()) & 0xffff),
  };
}

function header(size) {
  const buffer = new ArrayBuffer(size);
  return { buffer, view: new DataView(buffer), bytes: new Uint8Array(buffer), at: 0 };
}

function u16(h, value) {
  h.view.setUint16(h.at, value, true);
  h.at += 2;
}

function u32(h, value) {
  h.view.setUint32(h.at, value >>> 0, true);
  h.at += 4;
}

function raw(h, bytes) {
  h.bytes.set(bytes, h.at);
  h.at += bytes.length;
}

/** Pack ``[{path, file|blob}]`` into a ZIP ``Blob``. Calls ``onProgress(done, total)``. */
export async function zipFiles(items, onProgress) {
  const total = items.reduce((n, item) => n + (item.file?.size ?? item.blob?.size ?? 0), 0);
  let done = 0;
  const chunks = [];
  const central = [];
  let offset = 0;
  const names = new Set();
  for (const item of items) {
    let name = String(item.path || item.file?.name || "file").replace(/\\/g, "/").replace(/^\/+/, "");
    if (!name || name.endsWith("/")) continue;
    // Keep names unique inside the archive.
    let unique = name;
    for (let n = 1; names.has(unique); n += 1) {
      const dot = name.lastIndexOf(".");
      unique = dot > 0 ? `${name.slice(0, dot)} (${n})${name.slice(dot)}` : `${name} (${n})`;
    }
    names.add(unique);
    name = unique;
    const blob = item.file || item.blob;
    const bytes = new Uint8Array(await blob.arrayBuffer());
    const crc = crc32(bytes);
    const { time, date } = dosTime(new Date(item.file?.lastModified || Date.now()));
    const nameBytes = encoder.encode(name);
    const local = header(30 + nameBytes.length);
    u32(local, 0x04034b50);
    u16(local, 20);
    u16(local, 0x0800); // UTF-8 names
    u16(local, 0); // stored
    u16(local, time);
    u16(local, date);
    u32(local, crc);
    u32(local, bytes.length);
    u32(local, bytes.length);
    u16(local, nameBytes.length);
    u16(local, 0);
    raw(local, nameBytes);
    chunks.push(new Blob([local.buffer]), new Blob([bytes]));
    central.push({ nameBytes, crc, size: bytes.length, time, date, offset });
    offset += 30 + nameBytes.length + bytes.length;
    done += bytes.length;
    onProgress?.(done, total);
  }
  const centralStart = offset;
  let centralSize = 0;
  for (const entry of central) {
    const head = header(46 + entry.nameBytes.length);
    u32(head, 0x02014b50);
    u16(head, 20);
    u16(head, 20);
    u16(head, 0x0800);
    u16(head, 0);
    u16(head, entry.time);
    u16(head, entry.date);
    u32(head, entry.crc);
    u32(head, entry.size);
    u32(head, entry.size);
    u16(head, entry.nameBytes.length);
    u16(head, 0);
    u16(head, 0);
    u16(head, 0);
    u16(head, 0);
    u32(head, 0);
    u32(head, entry.offset);
    raw(head, entry.nameBytes);
    chunks.push(new Blob([head.buffer]));
    centralSize += 46 + entry.nameBytes.length;
  }
  const end = header(22);
  u32(end, 0x06054b50);
  u16(end, 0);
  u16(end, 0);
  u16(end, central.length);
  u16(end, central.length);
  u32(end, centralSize);
  u32(end, centralStart);
  u16(end, 0);
  chunks.push(new Blob([end.buffer]));
  return new Blob(chunks, { type: "application/zip" });
}

/** Read one FileSystemFileEntry as {path, file}. */
function readEntryFile(entry, path) {
  return new Promise((resolve, reject) => {
    entry.file(
      (file) => resolve({ path, file }),
      (err) => reject(err || new Error("Couldn’t read that file.")),
    );
  });
}

/** Walk a FileSystemDirectoryEntry, collecting files as {path, file}. */
async function walkDirectory(entry, prefix) {
  const out = [];
  const reader = entry.createReader();
  const batch = () =>
    new Promise((resolve, reject) => {
      reader.readEntries((entries) => resolve(entries), (err) => reject(err));
    });
  for (;;) {
    const entries = await batch();
    if (!entries.length) break;
    for (const child of entries) {
      if (child.isFile) {
        out.push(await readEntryFile(child, `${prefix}${child.name}`));
      } else if (child.isDirectory) {
        out.push(...(await walkDirectory(child, `${prefix}${child.name}/`)));
      }
    }
  }
  return out;
}

/**
 * Collect files (and folders, recursively) from a drop event.
 * Returns ``{files: [{path, file}], folders: number}``.
 */
export async function collectDropped(event) {
  const items = [...(event.dataTransfer?.items || [])];
  const getEntry = (item) => item.webkitGetAsEntry?.() || item.getAsEntry?.();
  if (!items.some((item) => item.kind === "file" && getEntry(item))) {
    return { files: [...(event.dataTransfer?.files || [])].map((file) => ({ path: file.name, file })), folders: 0 };
  }
  const files = [];
  let folders = 0;
  for (const item of items) {
    if (item.kind !== "file") continue;
    const entry = getEntry(item);
    if (!entry) {
      const file = item.getAsFile();
      if (file) files.push({ path: file.name, file });
      continue;
    }
    if (entry.isFile) {
      files.push(await readEntryFile(entry, entry.name));
    } else if (entry.isDirectory) {
      folders += 1;
      files.push(...(await walkDirectory(entry, `${entry.name}/`)));
    }
  }
  return { files, folders };
}

/** Common root name for the ZIP (the folder, or "files" for a mix). */
export function zipName(items) {
  const first = (items[0]?.path || "files").split("/")[0] || "files";
  const same = items.length > 0 && items.every((item) => item.path.split("/")[0] === first);
  const base = same ? first : "files";
  const clean = base.replace(/[^\w\- ()[\].]+/g, "_").slice(0, 60) || "files";
  return `${clean}.zip`;
}
