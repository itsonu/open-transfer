# Roadmap: features in phases

Tracking issue: [#47](https://github.com/itsonu/open-transfer/issues/47). Each phase lands as its own commits: **tests first** (they fail), then the code (they pass), then the whole suite (unit + browser end-to-end) before it's pushed.

Status legend: **Done** · **Partial** (some of it existed before this plan) · **New** · ✅ **Phase X** (shipped in that phase).

## Ground rules

- **No build step, no new runtime dependencies.** Plain ES modules and Flask, as today.
- **Client preferences** (favorites, view mode, sort) live in `localStorage`, guarded with `try/catch` (the app still works when storage is blocked). The desktop app and the Android app keep their web storage between launches.
- **Anything another device relies on** (clipboard sync permissions, transfer history, duplicate handling) lives on the app (server side), in its state folder.
- **Browser limits are respected, not worked around.** Reading the clipboard needs a user gesture and a secure context (`https://` or `localhost`); iPhone and iPad stay browser-only.
- **Tests:** Python behaviour → `tests/test_*.py`. UI behaviour → `tests/e2e/` (Playwright, real app processes).

---

## Phase A — Drag-and-drop and sending faster

| Feature | Before | Steps | Tests |
|---|---|---|---|
| **Device Drag-and-Drop** | ✅ Phase A | Kept. Dropping onto an offline device says why nothing happened. | Existing `test_drop_files_onto_a_device`; new: drop on an offline tile shows a message. |
| **Visual Device Drop Targets** | ✅ Phase A | 1. While dragging files, each online tile shows "Drop to send". 2. The tile under the pointer is highlighted and announced to screen readers ("Release to send to Pixel"). 3. Offline tiles dim. | Drag over a tile → it has `is-drop-target` and the hint; the live region names the device. |
| **Multi-Device Drag-and-Drop** | ✅ Phase A | 1. Dropping onto a **selected** tile sends to **every** selected device. 2. While dragging with 2+ devices online, an **"Everyone"** group target appears; dropping there sends to all online devices (after the usual "send to everyone?" check). | Select 2 devices, drop on one → both get the offer. Drop on "Everyone" → confirm dialog, then all get it. |
| **Nearby Device Favorites** | ✅ Phase A | 1. Star button on each tile (`aria-pressed`). 2. Favorites sort first and stay listed (dimmed) when offline. 3. Stored per browser/app in `localStorage`. | Star a device → it moves first and survives a reload; un-star → back in place. |
| **Quick Send** | ✅ Phase A | 1. A favorite's tile gets a **Send…** shortcut: pick files → they go straight to that device (no Send bar step). 2. `Alt + 1–9` opens the picker for the nth favorite (browsers keep `Ctrl/⌘ + 1–9` for switching tabs). | Quick send to a favorite → the receiver gets the offer with no Send click. |
| **Adaptive Device Layout** | ✅ Phase A | Grid sizes by width: 3 columns on phones, bigger tiles on touch tablets (incl. Galaxy Tab), auto on desktop; tiles ≥ 96 × 44 px, star ≥ 32 px and always shown on touch; the send bar never covers the last row. | Phone (390 px), tablet (800 px) and desktop (1280 px) viewports: no horizontal scroll, tile ≥ 44 px, expected columns. |

## Phase B — Previews and media

| Feature | Before | Steps | Tests |
|---|---|---|---|
| **Media Preview** | ✅ Phase B | 1. In-app preview dialog for images, video, audio, PDF and text. 2. ← / → between files, Esc closes, focus returns. 3. Server serves PDFs inline for the preview only (`Content-Disposition: inline`; the PDF viewer can't run sandboxed, so its CSP allows only the plugin and framing by the app itself). | Unit: `?inline=1` for PDF/video gives the right headers. E2E: open the preview for an image / text file; arrows move; Esc closes. |
| **Media Thumbnail Grid** | ✅ Phase B | Thumbnails for videos (first frame), and for local files in the send bar and transfer cards (object URLs, revoked after). | Video row shows a `<video>` thumb; staged image shows a thumb. |
| **Media Gallery View** | ✅ Phase B | 1. List ⇄ Grid toggle. 2. Filter: All · Photos · Videos · Documents. 3. Sort: newest · name · size. 4. Click → full-screen preview (Phase B dialog). Choices remembered. | Toggle grid → tiles; filter Photos → only images; sort by name → order. |
| **Quick Actions** | ✅ Phase B | A "⋯" menu (and right-click) per file: Preview, Copy, Save, Share (Web Share where supported), Reveal in folder (desktop/Android owner only). | Menu lists the expected actions per file type; keyboard opens it. |
| **Cross-Device File Operations** | ✅ Phase B | Owner-only API `POST /api/files/<name>/open` and `/reveal` (local machine only), using the OS opener; the Android bridge for its own. Programs and scripts are only ever revealed, never opened; machines without a desktop (servers, containers) don't offer either. | Unit: owner can call it (opener mocked); visitors get 403; path traversal rejected. |

## Phase C — Clipboard

| Feature | Before | Steps | Tests |
|---|---|---|---|
| **Copy to Clipboard** | ✅ Phase C | Copy images (as PNG), copy a file's link, `Ctrl/⌘+C` on a focused file row. | Copy button writes the expected text / link (clipboard permission granted in the test). |
| **Clipboard Sharing** | ✅ Phase C | "Send clipboard" in the send bar (and `Ctrl/⌘+Shift+V`): reads text or an image, stages it, sends to the chosen devices. | Grant clipboard read, put text in it, Send clipboard → receiver gets the `.txt`. |
| **Paste-to-Transfer** | ✅ Phase C | Pasting text outside an input stages it as a `.txt`; pasted images keep a readable name. | Dispatch a paste with text → a staged text file. |
| **Clipboard Privacy Controls** | ✅ Phase C | 1. Per-device "clipboard" permission (off by default; paired devices only). 2. Text that looks sensitive (passwords, one-time codes, card numbers, keys) needs confirmation before sending. 3. Excluding apps is only possible in the native apps; the browser can't see which app copied, so it's documented as unsupported there. | Unit: sensitive-text detector. E2E: sending a card-like number asks first. |
| **Shared Clipboard Sync** | ✅ Phase C | 1. App-to-app `clip` message between **paired** devices that both allowed it. 2. Sent text/images arrive without a prompt, go into clipboard history, and are written to the system clipboard when the window has focus. 3. Size cap (1 MB text, 10 MB image). 4. Optional auto-share while the window is open (browsers only allow reading the clipboard then); private-looking text is never auto-shared. | Unit: clip accepted only from paired + allowed peers; rejected otherwise; size cap. E2E: two apps, allow, push → it shows up on the other. |
| **Clipboard History** | ✅ Phase C | App-side list (last 50 items, configurable retention), search, copy again, delete, clear; sensitive items are never kept. | Unit: retention, search, no sensitive items. E2E: list + search + clear. |

## Phase D — Transfers, history and reliability

| Feature | Before | Steps | Tests |
|---|---|---|---|
| **Transfer Queue** | ✅ Phase D | New sends queue behind the ones in progress (2 at a time); pending ones can move up/down or be removed. | E2E: 3 sends → 1 queued; move it up; cancel it. |
| **Transfer Search and History** | ✅ Phase D | 1. App-side history (`history.jsonl` in the state folder, capped). 2. `GET /api/history?q=&device=&direction=&status=&since=`. 3. Search box + filters in Recent. | Unit: filters and cap. E2E: search by name, filter by device. |
| **Transfer Activity Timeline** | ✅ Phase D | One chronological view of transfers and clipboard events from the history API, grouped by day. | E2E: a send and a clipboard event appear in order. |
| **Duplicate File Detection** | Partial (auto-rename) | 1. The incoming prompt says how many files already exist (same name and size). 2. Choice: Keep both (rename) · Skip duplicates · Replace. | Unit: each policy on disk. E2E: prompt shows "1 already here". |
| **Transfer Integrity Verification** | New | SHA-256 computed by the sending app as it streams and by the receiving app as it writes; a mismatch fails the file clearly and removes it. | Unit: matching hash → done; tampered bytes → failed with an integrity message. |
| **Connection Quality Indicator** | ✅ Phase D | Per device: last seen, round-trip time from hellos, speed of the current transfer, and the path ("Direct" / "Via Mac"). | Unit: RTT recorded. E2E: job target shows the path. |
| **Transfer Notifications** | Partial (title + toasts) | System notifications (Notification API, Android bridge) when the window is hidden: incoming request (Accept / Decline), done, failed, clipboard received. Asked for on a user gesture. | E2E: notification permission granted, page hidden → a notification is created (stubbed). |

## Cross-cutting (every phase)

- **Keyboard Shortcuts:** `Ctrl/⌘+O` choose files · `Ctrl/⌘+Enter` send · `/` search · `Esc` close or cancel · `Alt+1–9` quick send (done in Phase A) · `?` shortcut list. Tested per phase as features land.
- **Accessibility Support:** every control has a name, focus is visible and returns after dialogs, drop targets and progress are announced, nothing needs a mouse. A browser test fails on unnamed buttons or images without `alt`.

## Not in scope

- **iOS app:** iPhone and iPad keep using the browser.
- **Reading another app's clipboard in the background from a web page:** browsers don't allow it.
