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
run page, signed in to GitHub; each download is a .zip): `open-transfer-windows-x64.exe`,
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
| 6.6 | Android: incoming offer while the app is in the background → notification | |

## If devices don't see each other

1. Same Wi‑Fi? Guest networks and many routers isolate devices.
2. Firewall: Windows → allow *Open Transfer* on **Private** networks; macOS →
   System Settings → Privacy & Security → **Local Network**.
3. Pair with **Add device → Enter a code → Can’t find it? Enter its address** using
   the address shown on the other device — this bypasses discovery.
4. Collect logs: desktop `…/Open Transfer/.open-transfer/open-transfer.log`;
   Android `adb logcat -d | grep -iE "python|open_transfer|AndroidRuntime"`.
