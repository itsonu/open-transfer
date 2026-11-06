# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Received text and links read in the list.** A short text someone sent shows its first line under its name, and one that holds a web address gets an **Open link** button (opens in your browser; only `http`/`https` addresses). Text that is just an address is sent as `link-….txt` rather than `note-….txt`.
- **Windows installer** (`open-transfer-windows-x64-setup.exe`) next to the portable .exe: installs for your user only (no admin prompt) to `%LOCALAPPDATA%\Programs\Open Transfer`, adds a Start menu entry (and a desktop one if you tick it) and an entry under Settings → Apps; installing a newer one updates in place, closing and reopening the app. Uninstalling turns off Start at login and keeps your files, pairings and history. CI installs it, starts the installed app and uninstalls it on every run. The README now says how to update and how to remove Open Transfer completely on each platform.
- **Android: Send what you copied sends a copied image.** The app serves it to the page the way it serves a shared photo, and it's staged as *Clipboard image ….png*.
- **Android: Accept or Decline from the notification.** When someone wants to send files while the app is in the background, the notification now has **Accept** and **Decline** buttons; you don't have to open the app. An offer already answered or expired says so.
- **Manage a device from its tile.** On your own device, each app's tile has a ⋯ menu (right-click works too). A paired device offers **Remove pairing**: it asks first, the other device is told and forgets this one, and history stays. An unpaired device offers **Pair…**, which opens Add device ready for its code.
- **Every transfer says how it's protected**, from its weakest hop: “encrypted” (paired apps, authenticated), “encrypted, not verified” (an unpaired app, or a direct browser transfer whose connection messages crossed plain HTTP), “encrypted in transit” (HTTPS to a browser, WebRTC between browsers), or “not encrypted” (a browser on plain HTTP, an older app).
- **New installations start with “Encrypted transfers only” on.** An existing installation keeps working with older apps (recorded once in `settings.json`); turn it on under Add device.
- **With `--https`, browsers can't use the plain port** (pages redirect; a page left open from an old link is told the HTTPS address). The device's own window, app-to-app traffic, health checks and iPhone Shortcuts are unaffected.
- **Unpairing tells the other device.** It forgets the pairing too and says “*X* unpaired this device. Pair again…”; if it was away, its next send says the same, with a **Pair again** button, instead of an encryption error. Transfers under way with that device stop and their keys are dropped; history stays.
- **Forward secrecy for transfers between apps (`ot-file/2`).** Each transfer now starts with its own X25519 handshake: both apps make a fresh key for it and throw it away as soon as the session key exists, and both drop the session key when the transfer ends. Someone who recorded a transfer and later steals the pairing key still can't read it. Between paired apps the handshake is authenticated by the pairing; each receiver in a group gets its own session; a changed, replayed or misdirected answer stops the transfer instead of sending it plain. Apps from earlier builds of this branch (`ot-file/1`) can't receive from this one until updated.
- **Direct browser-to-browser transfers can't be hijacked between the apps.** When the two browsers are on different apps, the apps pass their WebRTC connection messages along. For an encrypted transfer each message now carries a MAC under that transfer's key (the pair key, or the one-off key between unpaired apps), so nobody on the network can change the DTLS fingerprint or addresses in it, replay it, or move it to another transfer or receiver. Each browser's own hop to its app is still plain HTTP unless `--https`.
- **“Encrypted transfers only”** (a switch under Add device, or `--encrypted-only`): refuse to send files to, or take them from, apps that can't encrypt (older versions), and say what to do. Off by default, so older versions keep working, labelled **not encrypted**.
- **`--https`: an old `http://` link now goes to the HTTPS address.**
- **Transfers between unpaired apps are encrypted too,** with a one-off key (X25519 to the receiver's per-run key, then the same AES-256-GCM records). Someone listening on the Wi‑Fi can't read the files or their names. The other app isn't verified, so they're labelled **encrypted, not verified**; pair the devices to be sure. Older apps still get plain files, labelled.
- **`--https`: browsers over TLS.** A browser's own hop to its app was plain HTTP. With `--https` (`OPEN_TRANSFER_HTTPS=1`), the app also serves browsers over HTTPS on the next port, and links and QR codes use it. The certificate is the device's own (browsers warn once; the Add device sheet shows its fingerprint to compare), and its key is sealed in the system keystore. A visitor's page now says whether its connection is encrypted.
- **Pairing keys are kept in the system keystore:** the macOS Keychain, Windows DPAPI, the Android Keystore or the Linux Secret Service holds a vault key, and the pair keys and browser sign-in key in the state folder are sealed with it, so a backup or a copy of the folder opens nothing. Existing pairings are sealed on first start, nothing is dropped: a pairing whose key can't be unlocked is kept and named ("Pair again"). Docker and Linux without a keyring use a private file and say so. `open-transfer --keys` shows where they are; `--rotate-keys` reseals them with a new vault key. On an ad-hoc signed Mac build, macOS asks once after each update to let the app read its Keychain item. See [docs/encryption.md](docs/encryption.md#keys-at-rest).
- **Files between paired apps are encrypted end to end** (`ot-file/1`): AES-256-GCM in 64 KiB records, with a key per file derived (HKDF) from the pair's key. Changed, reordered, replayed or cut-off bytes are refused and nothing is kept. File names are hidden too. Group sends use each receiver's own key. Transfers that can't be encrypted (unpaired or older apps) are labelled **not encrypted**. An attempt to downgrade a paired transfer to plain text fails instead. Adds the `cryptography` dependency. See [docs/encryption.md](docs/encryption.md).
- **Android: share text and links to the devices you choose.** **Open Transfer** now appears in the share sheet for text and links too, next to **Send to my devices**. What you share is staged in the app, where you choose one or more devices. A link keeps its page title, and text shared with photos (a caption) is kept as well. Before, text and captions were dropped, and **Send to my devices** just failed when clipboard sync was off. Now it opens the app with the text ready to send.
- **Android: Share… a received file to another app** from its ⋯ menu (the system share sheet; Android's WebView has no Web Share).
- **Phone hotspots work, with or without internet:** a phone (or a Mac or PC) that *shares* the hotspot now announces itself, and shows its QR code, on the hotspot address instead of its mobile-data one. Where a hotspot or router drops multicast, the app looks through its own network for devices when entering a code finds nobody, and when it changes networks while a paired device is away; paired devices found that way still have to prove who they are. "Couldn't reach" now says whether Open Transfer isn't running there or the network keeps devices apart (client isolation, a firewall, macOS Local Network).
- **Trusted Sync Network (protocol):** devices that turn on clipboard sync form one network. Pair a new device with any one member and the others are introduced to it automatically (each pair gets its own key). Removing a device removes it from every member.
- **Sync rules:** each copy becomes an event with a unique id, a sequence number, a hybrid-clock time and an expiry. Duplicates and loops are dropped. An older clip never overwrites a newer one, and when copies happen at the same time on different devices, the newest one wins everywhere. A device that is away gets only the newest clip, and only until it expires (2 minutes by default).
- **Encrypted clips:** synced text is sealed for each recipient with that pair's key, in addition to the signed request.
- **Sync settings:** all trusted devices or selected ones; automatic or only when you ask; text and/or links; pause everything or one device; a temporary session; private-looking text and password-manager copies are never synced automatically; blocked apps.
- Settings API: `/api/sync` ([docs/api.md](docs/api.md)). The design and what each OS allows are in [docs/sync.md](docs/sync.md).
- **Clipboard sync on Mac, Windows and Linux with no window open:** the desktop app watches the system clipboard natively (macOS pasteboard, the Windows clipboard, wl-clipboard or xclip on Linux) and writes clips that arrive. Password-manager copies are recognised and never synced automatically; clips it writes are tagged so they never bounce back.
- **Runs in the background:** with sync on, closing the window keeps Open Transfer syncing; opening the app shows a window onto it. **Start at login** (a LaunchAgent on macOS that restarts after a crash, the Run key on Windows, autostart on Linux), a **menu-bar item** on macOS (open, pause for an hour, quit), and **Quit** in the page.
- **Clipboard sync sheet:** turn sync on, see whether it's working, pause it, choose all devices or selected ones, automatic or only when asked, text and links, what to do with passwords and codes, how long a clip waits for a device that's away, apps to leave out, start at login, recent activity (never the contents), remove a device from every device, or leave the network.
- `open-transfer --clipboard` syncs the clipboard from the command line too.
- **Clipboard sync on Android:** clips from your other devices go straight onto the phone's or tablet's clipboard in the background. To send, use the new **Send clipboard** Quick Settings tile, **Share → Send to my devices**, the text-selection menu, or just open the app (a new copy goes out as it comes to the front). Android 10+ only lets the app in front read the clipboard, so sending always takes that one step.
- **iPhone and iPad (Shortcuts):** add one in Clipboard sync and it gets a key for two Shortcuts: **Send clipboard** and **Paste from my devices**. Run them from Back Tap, the Action button, a widget or the share sheet. The device it was added on is its hub: it sends the iPhone's clips to the whole network and keeps the newest clip from any of your devices for it. iOS doesn't allow background clipboard sync, so this is the one-tap version.

- **Share photos and files from any Android app:** Open Transfer is now in the share sheet for photos, videos, audio, PDFs and any other file, one or many at once. The app opens with them ready to send: tap a device, then **Send**. Files stream straight from the app you shared from, so large videos work too. Text still goes through **Send to my devices**; a shared `.txt` file is sent as a file.
- **Compatibility tests** for all ten device pairs (Mac, Windows, Android phone and tablet, iPhone/iPad), both ways, plus one copy reaching a six-device network, removal, and expiry while a device is away; real-device checks in [docs/device-testing.md](docs/device-testing.md).

- **Scan a QR code to pair from the Mac and Windows apps and from Safari and Firefox,** not only on Android and in Chrome. Any camera works, built-in or USB, with **Use another camera** when there's more than one.

### Changed
- **Shared-folder mode: visitors can't delete files unless you allow it** (`--allow-delete` / `OPEN_TRANSFER_ALLOW_DELETE=1`). Before, anyone on the Wi‑Fi could. If your setup relies on visitors deleting, add the flag. `--no-delete` still works.
- **Pairing can't be cracked by listening** ⚠️ *pair again after updating; older apps can't pair with this one*: the code is now checked with SRP (a password-authenticated key exchange). Before, someone on the same Wi‑Fi could work out the code, and the pair's key, from the messages alone. Now nothing sent lets anyone test a guess, and each try is one guess. Entering a code no longer broadcasts anything derived from it.
- **A pairing code only works while Add device is open** on its device, and opening it shows a fresh one. Entering a code finds whichever nearby device has Add device open; pairing with an older app says to update it.
- Android: the background service runs as a *connected device* service, so Android 15+ no longer stops it after about 6 hours a day.

### Fixed
- **Reloading or closing the page dropped files you'd chosen but not sent yet** (and sends waiting in the queue) without a word. It now asks first, as it already did while a send was running.
- **Several texts sent in the same minute all read “note-2026-…-1333.txt”**, so they couldn't be told apart in Transfers, the queue or Activity. A text is now named after its first words (“Gate code 4411.txt”) and an address after its site (“Link example.com.txt”, as Android does).
- **A declined send, or one that couldn't reach its device, showed “Direct · encrypted, not verified” / “not encrypted”** though nothing had crossed the network; that label now appears only once something was sent.
- **Closing the window mid-send left the transfer stuck.** The page does the uploading, so once it closed nothing ended the send: the sender kept it “accepted” for ever and never wrote it to Activity. The sending app now ends it like the receiver does (4 minutes if nothing was sent, 10 minutes after the last byte otherwise), tells the receiver, and records “Failed — the window sending it closed. Choose the files again to send them.”
- **Quitting Open Transfer mid-transfer** (closing the desktop window, Cmd+Q) now ends what was under way as **Canceled** in Activity on both sides (“Open Transfer was closed before it finished”) and tells the receivers at once; before, the transfer vanished from the sender's history and the receiver waited.
- **Retrying a partly delivered send saved the first files twice** on a paired device (“photo (1).jpg”). A retry now skips files already there (same name and size); an unpaired device's prompt has **Skip** chosen for them.
- **Cancelled or abandoned big transfers left their staged pieces on disk** (a sparse file the size of the whole transfer). They're deleted when the transfer is cleared, and leftovers from a crash after a day.
- **Activity now says who didn't get it and why** (“PC: Nothing was sent…”, “Galaxy Tab: Declined”), and an offer to you that ran out reads **Not received** instead of “No answer”.
- **Activity showed a green “Received” for transfers that never arrived.** The badge now says what happened (“No answer”, “Declined”, “Failed” in red) and says Sent or Received only for transfers that finished.
- **A long transfer was cut off after an hour.** The receiver failed any transfer an hour after it was offered, however fast it was still going (a 10 GB video at 2 MB/s needs about 83 minutes). A transfer now fails only after 10 minutes without a new byte, saying “*Sender* stopped sending.” One that was accepted but never started now says “*Sender* didn’t start sending.” instead of “No answer”.
- **Android: a copied photo was sent as its `content://…` address.** Clipboard sync and *Send what you copied* turned a copied image or file into text; copied images are now sent as images, and clipboard sync skips them (it carries text and links).
- Between unpaired apps, a transfer right after the other app's multicast announcement could go unencrypted: the announcement made the app forget the other's key.
- With `--https`, a device whose name made the certificate's name longer than 64 characters couldn't start, and the half-started app never exited.
- **Security: a paired device can't be redirected by someone else on the Wi‑Fi.** One packet with its id used to move it to their address, so files meant for it went to them. A paired device's new address is now believed only when it proves it with the pair's key (after a Wi‑Fi reconnect or a new address, it is followed within seconds).
- **Windows: a firewall rule that blocks Open Transfer is named, and can be removed.** Cancelling Windows' own firewall prompt once leaves a rule that blocks the app whatever else is allowed. The warning now names it, and **Remove the block and allow** removes that rule (only rules for Open Transfer itself) in the same admin prompt.
- **Windows: Allow on this network no longer says it worked when Windows refused the rule.** It used to report success whatever happened inside the admin prompt.
- **One clipboard switch:** where the app can reach the clipboard itself, the old "Share automatically while this window is open" box is gone (and stops sharing); Clipboard sync is the switch. It used to sit beside the real one and be taken for it.
- **A device listed twice after a reinstall** now says which entry is probably the old copy, so you know which one to remove. Nothing is removed for you.
- A request refused before its body was read (wrong PIN, not the owner) could lose its answer on Windows; the server now reads small bodies first.
- **Android: Send what you copied works.** It always said to allow clipboard access, with nothing to allow; the app now reads the clipboard itself.
- **Every file is checked end to end again when the receiver has a PIN.** The receiving app asked the sending app for the PIN when it reported its checksum, so the check was quietly skipped.
- **Clipboard sync says who didn't take a clip:** a device with sync off used to look like it simply wasn't sent; Recently now names it ("MAYA didn't take it").
- **A PC others can't reach now says so, whatever the reason.** Devices that hear it but can't connect tell it when it says hello, and it names them ("Mac can't reach this device — usually its firewall"). Before, it only warned on a *Public* network, so a firewall rule made for Public left it silent on Private — and its advice to switch to Private was what broke it.
- **Windows: Allow on this network.** That warning has a button that asks Windows (one admin prompt) to let other devices reach Open Transfer on this kind of network: only its own port and discovery, never the firewall as a whole.
- **Windows: no more "Failed to remove temporary directory" when closing the window;** the copy that keeps syncing in the background now runs from its own files.
- **No more warning-coloured dot on a normal connection:** device tiles show the connection dot only when it's actually slow. A phone on Wi‑Fi is often a little slower than a computer, and that's fine.
- **Android: Preview on a PDF** no longer pops up the "Open with" app list; PDFs are previewed where the browser can show them, and saved or opened otherwise.
- **The drop zone's text lines up again** once files are listed (the title had drifted to the middle and the hint to the bottom-left).
- **The camera always turns off when scanning ends.** Closing Add device while the camera was starting (say, at the permission prompt) left it on, and tapping Scan twice opened it twice.
- **Camera problems say what's wrong:** access turned off, no camera, or the camera in use by another app, each with what to do. The code box stays ready.
- **On a Mac, Open Transfer could share its port with another app** listening on every address, so other devices reached the wrong app. It now picks a port nobody holds.
- **Pasting in Add device** fills in the code instead of queueing the text to send behind the sheet.
- **Pasting a pairing code** with other text around it ("Code: 332 156", a leading space) no longer cuts off digits.
- **Pairing with a Windows PC that others can't reach:** a device heard on the Wi‑Fi but not answering now shows **Can’t connect** on its tile, and pairing by code names it and says why (its firewall, usually a Wi‑Fi set to *Public*) instead of "No device on this network is showing that code".
- **Windows on a Public network:** the PC's own app now warns that other devices can't reach it and how to switch the Wi‑Fi to Private.
- The "Enter its address" field no longer shows an example address that looks filled in; it says where to find the other device's address.
- **One device, one tile:** a device that comes back with a new identity (an Android phone reinstalled from an APK signed with another key, which wipes its data) replaces its old pairing when you pair again, instead of showing up two or three times. Old duplicates already saved show once, and away devices keep their phone/tablet/computer icon.
- Paired apps can share clipboards with a device that has a PIN (the PIN is for people; paired apps sign their requests).
- **QR codes never contain the PIN:** with a PIN set, the share and pairing QR codes carry a token that lets a browser in for 10 minutes instead, so a photo of the screen isn't a lasting key. Changing the PIN cancels it. The terminal's QR code is just the address (the PIN is printed beside it). Typed `?pin=` links still work.
- **A fake goodbye can't hide a device:** a *bye* from an address other than the device's (someone else on the Wi‑Fi, or the device's second network interface) now makes the app check with the device first; it shows as gone only if it doesn't answer.

## [3.3.0] — 2026-10-22

### Added
- **Drop targets:** while you drag files, every nearby device shows "Drop to send", and the one under the pointer is announced to screen readers. Dropping onto a device that isn't around says so instead of quietly staging the files.
- **Send to several devices with one drop:** drop onto any selected device to send to all the selected ones, or onto the new **Everyone nearby** target (it asks first, like Send).
- **Favorites:** star a device to keep it first in the list, even while it's away.
- **Quick send:** a favorite gets a send button that goes straight to the file picker and sends to it, no Send bar step. **Alt+1…9** does the same for your first nine favorites.
- Bigger device tiles on tablets (iPad, Galaxy Tab), and the star is always visible on touch screens.
- **Preview** photos, videos, audio, PDFs and text files without leaving the app: ← and → step through the list, Esc closes. Text is always shown as plain text.
- **Thumbnails** for videos (their first frame), for photos waiting in the Send bar, on transfer cards and in Recent.
- **Gallery view:** switch the file list to a grid, show only photos, videos or documents, and sort by date, name or size. Your choice is remembered.
- **Quick actions:** a ⋯ menu (or right-click) on every file: Preview, Copy text / image / link, Save, Share, Delete, and on the device itself **Open** and **Show in folder**. Programs and scripts are only ever shown in their folder, never opened from here.
- **Paste to send:** paste text anywhere (not in a text box) and it's ready to send as a note; **Ctrl/⌘+Shift+V** or **Send what you copied** sends the clipboard (text or a picture).
- **Ctrl/⌘+C on a file** copies its text, the picture itself, or its link.
- **Clipboard sharing between paired devices:** a Clipboard panel on each app with **Share clipboard**, an optional auto-share while the window is open, searchable history (keep 20–200 items for 1 day to "as long as they fit"), and per-device switches. Both devices have to switch it on; anything else is refused. Clips travel signed with the pair's key.
- **Privacy checks:** text that looks like a password, one-time code, card number, private key or API token asks before it's sent or shared, is never auto-shared, is hidden on screen and is never saved to disk.
- **Activity:** "Recent" is now **Activity**: everything this device sent and received, plus clipboard shares, grouped by day, with search and filters by device and direction. The app keeps it (the last 1,000, private to your user), so it survives reloads and restarts. It records file names, sizes, who and how it ended, never what was in them.
- **Transfer queue:** two sends run at a time; more wait in line, and you can move them up or down or take them out.
- **Connection quality:** a dot on each nearby app shows how quick the connection is (round trip in ms), and each transfer says which way it goes: **Direct**, **Via** another device's app, or **Via this device**.
- **Files already here:** when someone sends a file you already have (same name and size), the Accept prompt says so and lets you **keep both**, **skip** it or **replace** yours.
- **Every file is checked:** apps compare a SHA-256 checksum of what was sent with what was written. A file that arrives damaged is removed and the sender is told to retry.
- **Notifications:** turn them on with the bell, and you'll get a system notification while the window is in the background when someone wants to send you files, a transfer finishes or fails, or a clipboard arrives.
- **Keyboard shortcuts:** `Ctrl/⌘+O` choose files, `Ctrl/⌘+Enter` send, `/` search, `Esc` clear what's waiting to be sent, and `?` lists them all.

### Fixed
- Two "Are you sure?" questions in a row could get the first one's answer (Cancel), or both get the second one's.
- Accepting with "skip the ones I have" could, for a moment, look to the sender like an ordinary accept; the receiver now settles which files it keeps before it says yes, and a sender with nobody left to send to asks once more before giving up.

## [3.2.5] — 2026-10-22

### Added
- Every release has a `SHA256SUMS` file, so you can check a download is exactly what was built (see the README).
- A command-line build for Intel Macs (`open-transfer-macos-x64-cli.tar.gz`), as there already was for Apple silicon.
- Homebrew: `brew install --cask itsonu/tap/open-transfer` (desktop app) or `brew install itsonu/tap/open-transfer` (command line).

### Fixed
- Windows: opening the app while it was already running could bring its window forward twice.

## [3.2.4] — 2026-10-18

### Fixed
- Resuming a transfer that was paused just as its last pieces arrived uploaded the whole file again, even though it had already been delivered (the progress restarted from 0 next to "Delivered"). The device now remembers files it has finished, so a resumed upload sees it is done; and if every piece is there but unconfirmed, only the last one is sent again. Same for big uploads to your own device, which could otherwise save a second copy (#44).

## [3.2.3] — 2026-10-18

### Added
- **Demo video** at the top of the README: a 20-second recording of the real app (a Mac sends two files to a Pixel phone and a Galaxy tablet; the phone taps Accept). The README shows a silent loop that plays inline and links to the full video with sound on the website. `scripts/demo/build.py` records it again after UI changes.

### Fixed
- Sending a file over 4 MB from one app to another failed with "Invalid upload" (since 3.2.0): the receiving device rejected the name of its own chunk store. Big files now arrive in chunks as intended.
- The sender's **Recent** list said "Received" for files sent to nearby devices, and showed a nonsense speed (e.g. "0.0169 bytes/s") because it mixed two clocks. It now says "Sent" with the real average speed.
- A device reached through a port forward (`adb forward`, `ssh -L`) could lose its real address: its hello arrives from 127.0.0.1, which replaced the address you added, so sending to it failed with "Lost connection" (since 3.0.0). A hello from loopback no longer replaces a real address.

## [3.2.2] — 2026-10-18

### Fixed
- A device that quit could show as nearby again for up to 15 seconds: a hello still on its way (or the discovery loop's last announcement) could land after its goodbye. Quitting now finishes those first and says goodbye last, refuses new hellos while stopping, and others ignore a hello answer that started before the goodbye.

## [3.2.1] — 2026-10-17

### Changed
- **New app icon** ("Beam"): a continuous-corner squircle in a cyan → blue → indigo gradient with faint discovery rings and staggered send/receive arrows, the same on the web app, website, Windows, macOS and Android. Android's launcher icon was a paper plane; it now matches, with an adaptive gradient background, the arrows as foreground and themed (monochrome) icon. `scripts/brand_icon.py` draws it in code and writes every icon asset.

### Fixed
- Releases no longer attach the Docker build record (`*.dockerbuild`) as a download; only the app, package and APK builds are published.
- The Android app reported version 3.0.0 (versionCode 300) since 3.0.0; it now carries the release version.

## [3.2.0] — 2026-10-16

### Added
- **Chunked transfers**: files over ~4 MB are split into 2 MiB `Content-Range` pieces (3 in flight, any order, idempotent rewrites) on every leg — browser to app (`POST /api/files`, `PUT /api/send/<job>/files/<n>`) and app to app (`PUT /api/p2p/v1/offers/<id>/files/<n>`) — with `GET …/status` endpoints reporting staged ranges, so a dropped connection loses pieces, not the file.
- **Pause / Resume** for chunked uploads (shared-folder and device sends), surviving page reloads via the stored upload id.
- **Folder sending**: drop or pick a folder and it is zipped in the browser (structure kept as paths, no dependency, offline) then sent through the normal pipeline.
- **Recent transfers** with per-transfer speed graphs (inline SVG sparklines, live during upload, kept for the last 30 in the browser).
- **Single-use guest links** (`POST /api/invites`, `/?invite=…`): one visitor past the PIN as a trusted guest, expiring after 10 minutes.
- `GET /api/info` limits now advertise `chunk_size` and `chunk_threshold`.

## [3.1.0] — 2026-10-15

### Added
- **Retry after decline**: declined/failed/expired transfers show a Retry button (per job and per device) backed by `POST /api/send/<job>/retry`, so the receiver gets a fresh Accept? prompt.
- **Share text**: the send bar has a Text button that sends typed text as a `.txt` file; received text files get a Copy button.

### Fixed
- `/api/info` no longer leaks the PIN to authenticated visitors (owner-only); proxy-spoofed owner bypass closed when `--behind-proxy` is used.
- P2P client no longer throws `UnboundLocalError` on connection errors; `ACCEPTED`/`RECEIVING` sessions expire instead of leaking; visitor/incoming/signal stores are bounded.
- Windows: long deduped Unicode names no longer fail deletes (`\\?\` extended paths); owner-only 403s drain the request body (no more connection resets).
- Web UI: API timeouts, safe WebRTC message parsing, receiver-side direct-transfer size caps, upload timeouts, QR encoding, dialog focus handling.

## [3.0.0] — 2026-10-15

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
- The Docker image runs on Python 3.14 (`python:3.14-slim`, was 3.13).
- GitHub release notes are now this changelog section (`scripts/release_notes.py`); a release fails if the section is missing or undated.
- CI and release workflows use the current major versions of their GitHub Actions (Node 24 runtime).
- README and website footers credit the maintainer with a link to their portfolio.

### Fixed
- Windows: deleting a file right after it was downloaded or previewed failed with a server error while the file was still open; it now waits a moment, or says the file is in use.
- Package `Changelog` URL pointed at a `main` branch that doesn't exist; it now uses `master`.

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

[Unreleased]: https://github.com/itsonu/open-transfer/compare/v3.3.0...HEAD
[3.3.0]: https://github.com/itsonu/open-transfer/compare/v3.2.5...v3.3.0
[3.2.5]: https://github.com/itsonu/open-transfer/compare/v3.2.4...v3.2.5
[3.2.4]: https://github.com/itsonu/open-transfer/compare/v3.2.3...v3.2.4
[3.2.3]: https://github.com/itsonu/open-transfer/compare/v3.2.2...v3.2.3
[3.2.2]: https://github.com/itsonu/open-transfer/compare/v3.2.1...v3.2.2
[3.2.1]: https://github.com/itsonu/open-transfer/compare/v3.2.0...v3.2.1
[3.2.0]: https://github.com/itsonu/open-transfer/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/itsonu/open-transfer/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/itsonu/open-transfer/compare/v2.0.0...v3.0.0
[2.0.0]: https://github.com/itsonu/open-transfer/releases/tag/v2.0.0
