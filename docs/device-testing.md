# Testing on real devices

CI covers a lot — unit and integration tests with several real devices in one
process, browser tests, the desktop apps opening their window on macOS and
Windows, and the Android app receiving a real transfer in an emulator. What it
can't cover is your actual Wi‑Fi, firewalls, OS permission prompts and
hardware. This checklist does. It takes about 20 minutes.

**You need:** a Windows PC and/or a Mac, an Android phone, a Samsung tablet (or
a second Android device), all on the **same Wi‑Fi** (not a guest network). An
iPhone/iPad is a bonus for the browser-only path.

**Install** from the [latest release](https://github.com/itsonu/open-transfer/releases/latest)
— or, before a release exists, from a green CI run's **Artifacts** (bottom of the
run page, signed in to GitHub; each download is a .zip): `open-transfer-windows-x64-setup.exe` (or the portable `open-transfer-windows-x64.exe`),
`open-transfer-macos-<arch>.dmg`, `open-transfer-android.apk` on both Android
devices. First launch: allow **Private networks** (Windows firewall), **Local
Network** (macOS), notifications (Android).

## 0. Let the script do the repetitive part (≈ 15–25 min)

`scripts/device_check.py` drives the **real apps** over your Wi‑Fi and fills in most
of sections 1–4 and 6 by itself: discovery, names/types, rename, an Android app
leaving and coming back, pairing by code in both directions (and exactly what a
QR scan sends), a photo and a 1 GB file in **every direction** between every two
devices (checked byte for byte, and on Android in Download/Open Transfer),
one / two / every device, decline, 2‑minute expiry, cancel, too big, a receiver
leaving mid‑transfer and *Send now*.

You need Python 3.10+ and `adb` ([platform-tools](https://developer.android.com/tools/releases/platform-tools))
on the computer. Then:

1. Open the Open Transfer app on the computer.
2. Connect each Android device to adb — USB, or **wireless debugging**:
   *Settings → Developer options → Wireless debugging → Pair device with pairing code*, then
   ```sh
   adb pair 192.168.1.23:37000      # the IP:port and code the phone shows
   adb connect 192.168.1.23:41000   # the IP:port on the Wireless debugging screen
   adb devices                      # each device listed as "device"
   ```
3. Run (from a checkout, or download just that file):
   ```sh
   python scripts/device_check.py --apk open-transfer-android.zip
   ```
   `--apk` takes the .apk or the CI artifact .zip and installs it on every connected
   device first. `--quick` uses a 100 MB file and skips the 2‑minute wait;
   `--serial <id>` limits it to some devices. With one Android device the
   one-to-many checks are skipped — connect the tablet too and run it again.

It writes **`device-check-<time>.md`** (✅ / ❌ per check + the checks that need a
person) and, if anything failed, Android logs next to it — send those files back.
Run it once on the Windows PC and once on the Mac. Notes:

* The script unpairs the devices it drives before it starts (the first transfers
  must ask), and leaves the computer paired with each Android device at the end.
* It deletes the test files it received unless you pass `--keep`.
* Phone → computer goes through `adb` for the button press, so with wireless
  debugging that direction is slower than real use.

Then do the rest by hand: everything marked 👤 in the results file, which are the
on-screen parts below (prompts, the 🔗 badge, the QR camera, drag and drop,
confirmation dialog, notifications, Wi‑Fi off/on, browsers, Windows ↔ Mac).

## By hand

Copy the tables into an issue and fill in ✅ / ❌ (+ a note or screenshot for ❌).

## 1. Discovery and identity

| # | Check | Win | Mac | Phone | Tab |
|---|-------|-----|-----|-------|-----|
| 1.1 | Each device lists every other one under **Nearby devices** within ~5 s | | | | |
| 1.2 | Names, icons (computer / phone / tablet) and platforms are right | | | | |
| 1.3 | Rename a device (✎ next to its name) → others show the new name within ~10 s | | | | |
| 1.4 | Close the app on one device → it disappears (or shows *Not nearby* if paired) on the others | | | | |
| 1.5 | Reopen it → it comes back without restarting the others | | | | |
| 1.6 | ⋯ on a paired device → **Remove pairing** → the other device says “… unpaired this device”; ⋯ → **Pair…** pairs again | | | | |

## 2. Pairing (QR and code)

| # | Check | Result |
|---|-------|--------|
| 2.1 | On the Mac/PC: **Add device** shows a QR code and a 6-digit code | |
| 2.2 | On the phone app: **Add device → Enter a code** → type the PC's code → *Paired with …* on both | |
| 2.3 | On the tablet app: **Add device → Scan its QR code** → scan the Mac's QR → paired | |
| 2.4 | A wrong code is refused; 5 wrong codes in a minute are rate-limited | |
| 2.5 | Paired devices show the 🔗 badge; sending between them doesn't ask to accept | |
| 2.6 | Pair the other way round (start from the phone, enter the code on the PC) | |

## 3. Transfers — every direction

Send a photo **and** a large file (≥ 1 GB video). For each row: the receiver
gets an **Accept / Decline** prompt (unless paired), progress shows on both
sides, the file opens correctly afterwards, and it's in **Downloads/Open Transfer**.

| From → To | Photo | 1 GB file | Notes |
|-----------|-------|-----------|-------|
| Windows → Android phone | | | |
| Android phone → Windows | | | |
| Windows → Mac | | | |
| Mac → Windows | | | |
| Windows → Samsung Tab | | | |
| Samsung Tab → Windows | | | |
| Mac → Android phone | | | |
| Android phone → Mac | | | |
| Mac → Samsung Tab | | | |
| Samsung Tab → Mac | | | |
| Android phone → Samsung Tab | | | |
| Samsung Tab → Android phone | | | |

**From the share sheet (Android).** In Photos (or Files), share one photo, then
several photos and a video together, then a PDF from Files: **Open Transfer** is in
the list each time; the app opens with them ready to send ("N files ready — choose
who to send to"); tap a device and **Send**; they arrive intact. Sharing *text*
shows only **Send to my devices** (not Open Transfer twice). Sharing a `.txt` file
from Files sends the file, not the clipboard.

## 4. One-to-one, one-to-many, everyone

| # | Check | Result |
|---|-------|--------|
| 4.1 | One device selected → only that device is asked; nobody else gets anything | |
| 4.2 | Select 2 of 3 devices → only those two are asked; per-device progress for each | |
| 4.3 | **Select all** → **Send to N** asks *Send to everyone nearby?* first; Cancel sends nothing | |
| 4.4 | Confirm → every device gets it; one declining doesn't stop the others | |
| 4.5 | Drop a file **onto a device tile** (desktop) → goes only to that device | |
| 4.6 | One receiver leaves (close app / Wi‑Fi off) mid-transfer → others still finish; that one shows *Failed* with **Retry** | |
| 4.7 | **Send now** while one device hasn't answered → the accepted ones get it | |
| 4.8 | Send bar → **Text** → paste a link → send: the receiver's Received list shows the address with **Open link**, which opens the browser (Android: the system browser) | |

## 5. Browsers without the app (iPhone / iPad / any browser)

| # | Check | Result |
|---|-------|--------|
| 5.1 | Scan a device's QR code with the iPhone camera → the page opens and shows the other devices | |
| 5.2 | iPhone → Windows app; Windows app → iPhone (*Sent to you* → Save) | |
| 5.3 | Two browsers (iPhone + another phone's browser) → transfer says **Delivered directly** | |
| 5.4 | Add the page to the home screen; it opens like an app | |
| 5.5 | A browser that only opened the link can't see the device's own files | |

## 6. States and robustness

| # | Check | Result |
|---|-------|--------|
| 6.1 | Turn Wi‑Fi off on a device → *Reconnecting…*; back on → *Reconnected*, list refreshes | |
| 6.2 | Decline → sender sees *Declined*; let an offer sit 2 min → *Didn't answer* | |
| 6.3 | Cancel from the sender mid-transfer → receiver shows it stopped; no half file in the folder | |
| 6.4 | Not enough space / file too big → declined up front with a reason | |
| 6.5 | Android: switch apps during a transfer → it keeps going (notification shown) | |
| 6.6 | Android: incoming offer while the app is in the background → notification; **Accept** on it starts the transfer, **Decline** tells the sender, without opening the app | |

## 7. Clipboard sync network

Turn on **Clipboard → Clipboard sync** on every device first. Design and limits: [sync.md](sync.md).

**Let the script do most of it** (≈ 2 min): on a Mac or PC with the desktop app open and your Android devices on adb (USB or wireless debugging, as in §0):

```sh
python scripts/clipboard_check.py                  # add --apk open-transfer-android.zip to install a build first
```

It pairs this computer with each Android device if needed, turns sync on, and checks with the **real clipboards**: this computer → Android (app in front, in the background, screen off), Android → this computer (share sheet, and the tile reading the phone's own clipboard), private text held back, your other computers (it shows text to copy there and asks what arrived) and syncing with the window closed. Results: `clipboard-check-<time>.md`.
`tests/test_sync_matrix.py` already runs all ten device pairs, both ways, with a stand-in clipboard; these checks are the real clipboards.

| # | Check | Result |
|---|-------|--------|
| 7.1 | Mac ↔ Windows with **both windows closed**: copy on one, paste on the other, both ways | |
| 7.2 | Copy on a computer → paste on the Android phone **and** tablet without opening the app | |
| 7.3 | Android → computer: the **Send clipboard** tile, Share → *Send to my devices*, the selection menu, and opening the app | |
| 7.4 | iPhone/iPad: *Send clipboard* and *Paste from my devices* Shortcuts (Back Tap) | |
| 7.5 | Pair a new device with **one** member → it syncs with all of them | |
| 7.6 | Remove a device on one member → gone on every member, gets nothing | |
| 7.7 | A password-manager copy and `password: test123` → **not** synced | |
| 7.8 | Start at login → restart → syncing without opening the app; macOS menu bar ⇄ (pause, open, quit) | |
| 7.9 | Android locked for an hour+ → clips still arrive (else: battery → Unrestricted; note the brand) | |
| 7.10 | Battery after a day (Android settings → Battery) | |

## 8. Phone hotspots and switching networks (no internet needed)

Turn **mobile data off** on the hotspot phone for at least one run, to prove
nothing needs the internet. Pair the devices once on ordinary Wi‑Fi first;
Use *paired* devices so transfers are encrypted (§3 should show
**encrypted** next to *Direct*).

| # | Check | Result |
|---|-------|--------|
| 8.1 | Android phone hotspot → **Mac** joins it: they see each other, pair (QR and code), send a file both ways | |
| 8.2 | Android phone hotspot → **Windows PC** joins: same as 8.1 (Windows will ask Public/Private: choose Private, or allow when Open Transfer warns) | |
| 8.3 | Android phone hotspot → **another Android phone or a Samsung Tab** joins: same as 8.1 | |
| 8.4 | **Mac** shares its connection (Internet Sharing over Wi‑Fi) → Android phone/Tab joins: same as 8.1. The Mac's QR code shows its 192.168.2.x address | |
| 8.5 | **Windows Mobile Hotspot** → Android phone/Tab joins: same as 8.1. The PC's QR code shows 192.168.137.1 | |
| 8.6 | Ordinary Wi‑Fi router → every device: same as 8.1 | |
| 8.7 | Paired Mac and phone on home Wi‑Fi → Mac switches to the phone's hotspot **without restarting either app** → within about 20 s both show each other online again; one tile each, no duplicates | |
| 8.8 | Turn the hotspot off and on again (new DHCP lease) → both come back online by themselves | |
| 8.9 | Two clients on one hotspot that can't reach each other: Add device → Enter its address says **"Nothing answered … client isolation"**; the device *sharing* the hotspot still reaches both | |

What makes this work, and the limits:
* A device that **is** the hotspot announces itself on the hotspot
  interface. Before H7 it used only the route out (mobile data), so its QR
  code and announcements pointed nowhere.
* Where the hotspot drops multicast, an app whose network changed while a
  paired device is away asks every address of its /24 for `/info` (about 250
  short requests, once per network change). Entering a code with nothing
  found does the same. Paired devices found that way must still prove it's
  them (N2) before anything goes to the new address.
* **Client isolation** (clients can't reach each other) is the network's
  doing, not Open Transfer's. The device sharing the hotspot can still reach
  everyone.

## 9. Android share sheet (into and out of Open Transfer)

Each row: share from the named app → pick **Open Transfer** → the items are staged → choose one device, then two devices → **Send** → the receiver gets exactly what was shared (names, contents) and the sender shows *Delivered*.

| # | Check | Result |
|---|-------|--------|
| 9.1 | One photo from Photos/Gallery | |
| 9.2 | Five photos and a video at once (SEND_MULTIPLE) | |
| 9.3 | A PDF and a .docx from Files / Drive | |
| 9.4 | A 2 GB video: staged without the app slowing down; progress shown; arrives intact | |
| 9.5 | Plain text from Keep/Notes → arrives as *Shared text ….txt* | |
| 9.6 | A link from Chrome → arrives as *Link ….txt* with the page title and the address | |
| 9.7 | Photos with a caption (e.g. from a messaging app) → the photos **and** a .txt with the caption | |
| 9.8 | Copy text → share it via the clipboard tile / *Send to my devices* with clipboard sync **off** → the app opens with the text ready to send | |
| 9.9 | The same photo shared twice in one go → staged once | |
| 9.10 | Share, then press Home before sending; reopen → still staged, sends fine | |
| 9.11 | Share from an app, then cancel in Open Transfer → nothing is sent | |
| 9.12 | A received file → ⋯ → **Share…** → the system share sheet → send it to another app (Gmail, Drive) | |
| 9.13 | A paired receiver: the transfer shows **encrypted** (§3) | |
| 9.14 | Copy a photo (Photos → Copy) → Open Transfer → **Send what you copied** → it arrives as an image, not as a `content://` line; with clipboard sync on, copying a photo syncs nothing | |

## 10. Keys at rest, unpaired encryption and `--https`

1. **Mac, after an update** of an ad-hoc signed build: macOS asks once to let
   Open Transfer use its Keychain item → **Always Allow** → paired devices are
   still paired (`open-transfer --keys` from source, or Add device shows them).
   Click **Deny** instead on a second update → the status line names the
   locked pairings (“Couldn’t unlock the pairing with …”); allow it again →
   they're back after a restart.
2. **Windows, after replacing the .exe:** paired devices still paired
   (DPAPI is per user, not per build).
3. **Android, after an in-place update** (same signing key): still paired.
4. **Unpaired send** Mac ↔ phone: the sender shows **Direct · encrypted, not
   verified**; hovering says to pair to be sure.
5. **`--https`:** start the Mac with `--https`, scan its QR code with the
   phone's browser → one certificate warning → its fingerprint (browser's
   certificate details) matches the one under the QR code → proceed → the
   page says **encrypted (HTTPS)**; send a photo from that page.

## 11. When things go wrong (one round per platform pair)

Automated in CI with real app processes; these check the real windows, apps and networks.

| # | Check | Result |
|---|-------|--------|
| 11.1 | Start a send from the **desktop app**, close its window before the other device accepts, accept there → within ~4 min both say it didn't happen (sender: *Failed — the window sending it closed*; receiver: *… didn't start sending*), and both Activity lists show it | |
| 11.2 | Close the desktop window **partway** through a 1 GB send → the receiver says it stopped (at once if the app quit; otherwise within 10 min); no half file in Downloads/Open Transfer | |
| 11.3 | Quit the **receiving** app (Cmd+Q / Quit) mid-transfer → its Activity says *Canceled — Open Transfer was closed*; the sender shows the failure; reopening leaves no partial file | |
| 11.4 | Force-stop the Android app mid-transfer (Settings → Apps → Force stop) → the sender fails it with a reason; Android's Download/Open Transfer has no partial file after reopening | |
| 11.5 | A paired device gets 2 of 5 photos, then Wi‑Fi drops; **Retry** when it's back → only the missing 3 arrive (no “photo (1).jpg”) | |
| 11.6 | Send 5 files to two devices; one declines → Activity shows *Partly sent*, the names, and “*device*: Declined”; searching one file's name finds it | |
| 11.7 | Choose files, then close the browser tab before sending → the browser asks before leaving | |


1. Same Wi‑Fi? Guest networks and many routers isolate devices.
2. Firewall: Windows → allow *Open Transfer* on **Private** networks; macOS →
   System Settings → Privacy & Security → **Local Network**. A Windows PC whose
   Wi‑Fi is set to **Public** can be *seen* by others but not *reached* — its
   own app now says so under its name, and the others show it as **Can’t
   connect**. Fix: Settings → Network & internet → Wi‑Fi → your network →
   **Private network**. Until then, pair *from* the PC (enter the other
   device's code there).
   Quick test from the Mac: `curl http://<pc-address>:5000/api/health` should
   answer `{"ok": true…}`; a timeout means the PC's firewall is blocking it.
3. Pair with **Add device → Enter a code → Not found? Enter its address** using
   the address shown on the other device — this bypasses discovery.
4. The same device shown several times ("Not nearby")? Each was a pairing with an
   identity it no longer has — on Android, installing an APK signed with a
   different key wipes the app. Pair again once: the old ones are removed.
   CI builds without the release-key secrets are signed with a new key each
   run; set `ANDROID_KEYSTORE_BASE64` and friends so updates install in place.
5. Collect logs: desktop `…/Open Transfer/.open-transfer/open-transfer.log`;
   Android `adb logcat -d | grep -iE "python|open_transfer|AndroidRuntime"`.
