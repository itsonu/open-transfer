# Open Transfer for Android (phones and tablets)

A thin native shell around the same Python package the desktop app runs. Every device,
Android or not, runs the same HTTP server, nearby-device discovery and transfer code
from [`src/open_transfer`](../src/open_transfer). The Android app adds only what a web
page can't do by itself.

```
┌────────────── MainActivity ──────────────┐     ┌──────── TransferService ────────┐
│ WebView → http://127.0.0.1:<port>/        │     │ foreground service (dataSync)   │
│ native loading / error screen             │     │ Wi-Fi + multicast locks         │
│ file picker, QR scanner, "open file"      │◀───▶│ Chaquopy: open_transfer.android │
│ JS bridge: window.OpenTransferAndroid     │ port│   start() / stop()              │
└───────────────────────────────────────────┘     │ events → notifications, media   │
                                                  │          scanner                │
                                                  └─────────────────────────────────┘
```

## How it works

* **`TransferService`** starts Python with Chaquopy and calls
  `open_transfer.android.start(storageDir, stateDir, deviceName, form, listener)` on a
  background thread. It holds a `WifiManager.MulticastLock` (without it, most phones
  drop the UDP multicast that discovery uses) and a Wi-Fi lock. Its persistent
  notification ("Open Transfer — visible to nearby devices") has a **Stop** action. The
  service, not the activity, owns Python, so rotating the screen, resizing it (split
  screen, DeX) or leaving the app doesn't restart anything.
* **`MainActivity`** starts the service, shows a native loading screen, polls
  `http://127.0.0.1:<port>/api/health`, then shows the UI full screen in a WebView.
  Edge-to-edge insets are applied as padding. Links to anything other than
  `127.0.0.1`/`localhost` open in the browser.
* **Storage:** received files go to `Download/Open Transfer`, visible in the Files app.
  Android 11+ lets an app create and read its own files there through plain paths. On
  Android 10 this needs `WRITE_EXTERNAL_STORAGE` and `requestLegacyExternalStorage`. If
  the folder isn't usable (for example, the permission was denied), the app falls back
  to `Android/data/io.github.itsonu.opentransfer/files/Download/Open Transfer`. The
  device identity, paired-device keys and session secret are kept privately in
  `files/open-transfer`.
* **Python events:** `offer` shows a notification when the app isn't on screen. Tapping
  it opens the app, where the web UI asks to accept. `received` runs the media scanner,
  so photos and videos show up in Gallery.
* **JavaScript bridge** (`window.OpenTransferAndroid`):
  * `scanQr()` opens the Google code scanner (Play services; no camera permission
    needed). The result arrives as
    `window.dispatchEvent(new CustomEvent('ot-native-scan', {detail: text}))`, with
    `detail: null` on cancel or failure.
  * `openFile(name)` opens `Download/Open Transfer/<name>` in another app through a
    `FileProvider`. Names containing `/` or `\`, or starting with `.`, are refused.
  * `platformInfo()` returns `{"form","model","manufacturer","sdk"}` as JSON.
* **`<input type=file multiple>`** uses the system document picker
  (`OpenMultipleDocuments`). Downloads of `/files/<name>` open the file directly, since
  it is already on the device. Other downloads, such as "Download all" (a zip), go
  through `DownloadManager`.

## Build

The Python sources are packaged straight from the repository's `src/` directory
(`chaquopy.sourceSets.main.srcDir("../src")`). pip installs `flask`, `cheroot` and
`segno` at build time. Keep them in sync with `pyproject.toml`.

Requirements:

* JDK 17+, Android SDK (set `ANDROID_HOME` or open the project in Android Studio).
* **Python 3.12 on the build machine** (`python3.12` on `PATH`, or
  `CHAQUOPY_BUILD_PYTHON=/path/to/python3.12`). Chaquopy needs the same major.minor as
  the app to run pip and compile `.pyc` files.
* Gradle 8.14.x. No wrapper JAR is committed, so use an installed `gradle` or let
  Android Studio provide one.

```sh
gradle -p android assembleDebug          # android/app/build/outputs/apk/debug/
gradle -p android assembleRelease        # signed with the debug key unless configured
gradle -p android testDebugUnitTest      # JVM unit tests (LocalPolicyTest)
```

In Android Studio: **File › Open…** and pick the `android/` folder.

Release signing reads `ANDROID_KEYSTORE_FILE`, `ANDROID_KEYSTORE_PASSWORD`,
`ANDROID_KEY_ALIAS` and `ANDROID_KEY_PASSWORD` (the store password is used if the key
password is unset). Without them, the release build is signed with the debug key so
the APK still installs.

32-bit ARM: Chaquopy only ships Python 3.12+ for 64-bit ABIs, so the default APK
contains `arm64-v8a` and `x86_64` (the emulator). For old 32-bit phones, build with
`-PopenTransfer.python=3.11`, which also adds `armeabi-v7a`.

### Versions

| Component | Version |
|---|---|
| Android Gradle plugin | 8.13.2 |
| Gradle | 8.14.3 |
| Kotlin | 2.3.0 |
| Chaquopy | 17.0.0 (Python 3.12) |
| compileSdk / targetSdk / minSdk | 36 / 36 / 29 |

Chaquopy 17.0 supports AGP 7.3–8.13 according to its own docs. Kotlin 2.3.0 is tested
with AGP up to 8.13.0 and Gradle up to 9.0.

## CI

[`.github/workflows/android.yml`](../.github/workflows/android.yml) has two jobs:

1. **build:** builds the release APK and runs the unit tests, then uploads
   `open-transfer-android.apk` as the `open-transfer-android` artifact.
2. **emulator-smoke:** installs the APK on an API 34 x86_64 emulator, launches it,
   waits for `/api/health` over `adb forward` and checks `/api/state` says
   `"platform":"android"`. It then runs
   [`ci/smoke_transfer.py`](ci/smoke_transfer.py), a real transfer of a ~1 MB file from
   the phone to an Open Transfer node running on the CI machine (reached from the
   emulator as `10.0.2.2`).

## Known limitations

* **Background time:** with `targetSdk` 35+, Android 15+ gives `dataSync` foreground
  services about 6 hours per day in the background. After that, the service stops
  itself (`onTimeout`) and the device stops being visible until the app is opened
  again. The `connectedDevice` service type has no such limit and may fit this app
  better, but it needs a Play-policy justification.
* **Sleep:** the service keeps Wi-Fi awake, not the CPU. On some phones, transfers
  slow down or pause while the screen is off for a long time.
* **Scoped storage:** after a reinstall, Android no longer counts earlier files in
  `Download/Open Transfer` as this app's own. The app can't list, open or overwrite
  them (new files get a unique name).
* **Process death:** if Android kills the app under memory pressure, the service is not
  restarted automatically (`START_NOT_STICKY`). Opening the app starts it again.
* The QR scanner needs Google Play services. Without them, `scanQr()` reports `null`.
* Lint is not run during release builds (`checkReleaseBuilds = false`). Run
  `gradle -p android lint` locally.
