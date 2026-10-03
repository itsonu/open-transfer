# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Transfer history** (`<state>/history.db`, SQLite, owner-only file): every transfer the device's owner sends or receives is recorded with sender, each recipient's result, files, sizes and timestamps. A group send is one record with a result per recipient (`partial` when only some received it). Records survive restarts (unfinished transfers become `failed`, reason `app_restart`) and outlive the files they mention. Retention: 1000 records / 180 days. API: `GET /api/history`, `DELETE /api/history/<id>`, `POST /api/history/clear`.
- **One transfer lifecycle with reasons and deadlines** (`lifecycle.py`): every state change is checked (a final state can't be overwritten); every unhappy ending has a `reason_code`, `reason` and `action`; accepted transfers with no first byte fail after 90 s and stalled ones after 60 s on both sides; a new `partial` state when only some files arrived.

- **Group sends are one transfer with a result per device**: the live view and history give every recipient its own state, reason, delivered/failed files and timestamps, plus a `summary` ("delivered to 2 of 3", declined, failed, disconnected…). The sender sees one card per send ("Sending photo to 3 devices" → "Delivered to 2 of 3") with expandable per-device results, and a toast whenever only some devices got it.

- **Secure pairing (SRP) with confirmation** — see [docs/trust-model.md](docs/trust-model.md): the 6-digit code is the password of an SRP-6a exchange, so watching the network reveals nothing about it; the code only works while *Add device* is open; the device showing it asks its owner "Pair with …?"; QR codes hold only the code and device id (no PIN).
- **Paired devices prove who they are**: answers to signed requests are signed too, and a paired device's address only changes after it proves the key at the new address; multicast `bye`/announce can no longer redirect or knock out a paired device.
- **Pairing without multicast**: a device reachable over HTTP can be paired by code alone (or chosen from a list); errors say what's really wrong (`discovery_unavailable`, `pairing_not_open`, `pairing_code_invalid`, `pairing_expired`, `trust_rejected`, `rate_limited` …) with a suggested action.
- **Unpairing is mutual**, and a device that missed it learns on next contact. Reinstalled devices show the old entry as "(old device)", same-name devices get a short id, and offline or old devices can be removed. Offline paired devices keep their type and show "last seen".

### Changed
- Pairing protocol v2 (`/pair/begin`, `/pair/prove`, `/pair/status`) replaces v1 (`/pair`, `/pair/confirm`); request signatures carry a nonce. Devices on an older 3.0 pre-release must be updated to pair.

### Fixed
- Code-only pairing failed with "No device on this network is showing that code" whenever multicast didn't reach (seen on a real Wi-Fi network), even though the device was visible.
- The multicast pairing lookup leaked a hash of the code (instantly reversible), and an observed pairing could be brute-forced offline.
- An unsigned hello or announce could re-point a paired device's address or rename it.
- Canceling one recipient in the middle of a group upload no longer holds up the others' upload for up to 2 minutes.
- A sender no longer records a failure when the receiver actually got the file (the reply was lost, or a cancel raced the last byte): it asks the receiver first.
- A multi-file transfer where one file failed no longer marks the whole recipient `failed` (it is `partial`), and no longer leaves the receiver "receiving" forever.
- Accepting without enough space now declines with the reason instead of leaving the offer waiting until it expires.
- A sender whose offer watcher never ran still expires the offer (deadline checked by housekeeping), instead of waiting forever.
- Closing the app during a transfer records `failed`/`app_closed` (not "canceled") and tells the other side.

## [3.0.0] — unreleased

Open Transfer becomes AirDrop-like across platforms: every device runs the app, devices find each other, and files go **directly to the devices you pick** instead of into one shared folder.

### Added
- **Nearby devices**: automatic discovery on the LAN (UDP multicast `239.255.77.77:47823` plus HTTP hellos), live presence, device name/type/platform, rename this device.
- **Targeted sending**: one, several or all devices; recipients shown before sending; confirmation before sending to everyone (or 3+ devices); drop files onto a device to send to it.
- **Accept / Decline** prompts on the receiver with sender and file list; offers expire after 2 minutes; disk space and size limits checked up front.
- **One-to-many streaming**: one upload is streamed to every accepting receiver at once, with per-receiver progress, cancel, retry and "send now"; a slow or vanished receiver is dropped without stopping the others.
- **Pairing** with a QR code or 6-digit code (HMAC challenge–response, rate-limited, rotating codes); paired devices auto-accept; requests between them are signed; `--paired-only`; unpair.
- **Browsers without the app** join a device's group by link/QR code, get their own "Sent to you" inbox, and can send to any device. **Browser → browser transfers go direct over WebRTC** (the apps only pass on the connection messages), falling back to the apps when a direct connection isn't possible; files up to 1 GB.
- **Desktop apps** with a native window (pywebview): `open-transfer-windows-x64.exe`, and `.app` bundles in `open-transfer-macos-arm64.dmg` / `open-transfer-macos-x64.dmg`; single instance per folder; smoke-tested in CI.
- **Android app** (phones and tablets, Android 10+, 64-bit) built with Chaquopy around the same Python code: WebView UI, native file picker, QR scanner, notifications for incoming files, saves to `Download/Open Transfer`; built and smoke-tested with a real transfer in an emulator in CI.
- **Real-device check** `scripts/device_check.py`: installs the APK over adb and drives the real apps on a computer, phones and tablets through the [device checklist](docs/device-testing.md) (discovery, pairing both ways, every transfer direction byte for byte, one/many/everyone, decline/expiry/cancel), writing a ✅/❌ report.
- CLI flags `--name`, `--form`, `--peer`, `--no-discovery`, `--auto-accept`, `--paired-only`, `--share-folder`; docs: [protocol](docs/protocol.md), updated [architecture](docs/architecture.md) and [API](docs/api.md).

### Changed
- **Default mode is now device-to-device.** Browsers that open a device's link no longer see its folder; use `--share-folder` for the classic shared-folder behaviour (the Docker image keeps it on by default, plus `--auto-accept`).
- Requests from the device itself (`127.0.0.1`) are its owner: no PIN needed, sees received files and the Accept prompts.
- The front end is split into ES modules (`lib.js`, `nearby.js`, `app.js`); still no build step.
- The macOS command-line build is now `open-transfer-macos-arm64-cli.tar.gz`; Windows also ships `open-transfer-windows-x64-cli.exe`.

### Fixed
- Windows: deleting a file right after it was downloaded or previewed failed with a server error while the file was still open; it now waits a moment, or says the file is in use.
- Package `Changelog` URL pointed at a `main` branch that doesn't exist; it now uses `master`.
- README and website footers credit the maintainer with a link to their portfolio.

### Known limitations
- Transfers between devices are plain HTTP on the LAN (not encrypted).
- Browser → app transfers still pass through the app the browser joined; no folder transfers; no resume.
- Apps are not code-signed/notarised; the Android APK is signed with a debug key unless release secrets are configured.

## [2.0.0] — 2026-09-24

The project is now **Open Transfer**: a rewrite of the original Flask "File Transfer" app into a complete, installable product. Existing URLs (`/upload`, `/downloads`, `/download/<name>`, `POST /transfer`) keep working.

### Added
- Single-page interface: drag & drop anywhere, paste files, multi-file queue with live speed/ETA, cancel and retry, file list with thumbnails, search, **Download all** as ZIP, delete with **Undo**, automatic dark mode, phone-friendly layout, accessibility (keyboard, labels, live regions, reduced motion).
- **Add a device** sheet with QR code, copyable link and other addresses; QR code in the terminal.
- Live updates across devices via `ETag` polling, with offline/reconnecting states.
- Optional **PIN** (`--pin`, auto-generated if no value) with rate limiting; the QR code signs people in.
- `--receive-only`, `--read-only`, `--no-delete`, `--max-size`, `--public-url`, `--allow-host`, `--behind-proxy`, `--verbose`; every option also as an `OPEN_TRANSFER_*` environment variable.
- JSON API (`/api/files`, `/api/info`, `/api/archive`, …) documented in `docs/api.md`.
- One-command runners (`run.sh`, `run.ps1`), `Makefile`, Dockerfile + Compose, PyInstaller spec, CI (Linux/macOS/Windows, Python 3.10–3.14, browser tests, Docker smoke test), CodeQL, pip-audit, Dependabot, release workflow.
- Test suite: storage, API, security and CLI unit tests plus Playwright end-to-end tests.
- Documentation: README, architecture, self-hosting, API, contributing, security policy, code of conduct.
- Project website (`site/`, published to GitHub Pages by `.github/workflows/pages.yml`).
- **Standalone app**: `python scripts/build_app.py` builds a single double-clickable executable (~13 MB, no Python needed to run) and smoke-tests it. Releases ship it for Windows (`.exe`), macOS and Linux (`.tar.gz`); CI builds and launches all three on every pull request. It saves to `~/Downloads/Open Transfer` by default and keeps its window open after a start-up error.

### Changed
- Uploads stream to disk (constant memory, no temp copy) via the cheroot server, and are published atomically; duplicate names get ` (1)` suffixes instead of overwriting.
- LAN address detection now finds the real interface instead of `127.0.1.1`; the next free port is used if 5000 is busy (macOS AirPlay).
- New icon and visual design; manifest fixed (icons were nested incorrectly).

### Fixed
- Path traversal in uploads and downloads (`../` in file names).
- Files with the same name silently overwrote each other.
- `/shutdown` crashed (`threading.join` does not exist).
- The service worker cached the home page forever; it has been removed.

### Security
- PIN attempts are rate-limited atomically (5 per minute per client), including the QR sign-in link; log output escapes client-supplied text.
- CSRF and DNS-rebinding protection, strict Content-Security-Policy and hardening headers, sandboxed downloads, hidden files and symlinks are never served.

### Removed
- Unrelated packet-sniffing and network-scanning scripts (`get.py`, `get2.py`, `scan.py`), the duplicate `backup.py`, the unused Tkinter import and IDE settings.

[Unreleased]: https://github.com/itsonu/open-transfer/compare/v3.0.0...HEAD
[3.0.0]: https://github.com/itsonu/open-transfer/compare/v2.0.0...v3.0.0
[2.0.0]: https://github.com/itsonu/open-transfer/releases/tag/v2.0.0
