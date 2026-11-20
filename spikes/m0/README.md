# M0 spike tools (throwaway)

For the **M0 test matrix** in [docs/offline-mesh.md](../../docs/offline-mesh.md#m0-test-matrix-the-four-scenarios-on-the-target-devices).
Not part of Open Transfer: they only answer "does this platform allow it, and how fast?".
Delete this folder once M0's results are written down.

| Tool | Runs on | Does |
|---|---|---|
| `android/` (app *OT M0 spike*, `io.github.itsonu.opentransfer.m0`) | Android 10+ | capabilities (STA+AP / STA+STA, advertising, its interfaces), host a **LocalOnlyHotspot**, **join** a network (Android's dialog), TCP sink/send, Bluetooth serve/measure. Log on screen and in `adb logcat -s OTM0` |
| `mac/otble.swift` + `mac/build.sh` | Mac | Bluetooth serve/measure (must run as its own app: `build.sh` makes `otble.app`) |
| `mac/otwifi.sh` | Mac | join a hotspot from the command line, status, leave (**drops the Mac off its Wi‑Fi until `leave`**) |
| `windows/m0.ps1` | Windows 10/11 | capabilities, **Wi‑Fi Direct legacy AP** and **Mobile Hotspot** hosting, join, leave, TCP sink/send |
| `tcp_speed.py` | Mac, Linux | TCP sink/send on :47900, default gateway |

Build the Android app: `gradle -p spikes/m0/android assembleDebug` (same plugins as `android/`).

## Matrix row → commands

| Row | Host | Joiner | Speed |
|---|---|---|---|
| S1a P → M | phone app: *3. Host LocalOnlyHotspot* (note SSID / passphrase) | `mac/otwifi.sh join <ssid> <pass>`; `otwifi.sh status` shows the gateway | Mac: `python3 tcp_speed.py send gateway 1024` with the phone app's *5. TCP sink* |
| S1b P → W | as S1a | `.\m0.ps1 join <ssid> <pass>` | `.\m0.ps1 send <gateway> 1024` |
| S2a W → P | `.\m0.ps1 wfd-host`, else `.\m0.ps1 hotspot` | phone app: SSID + passphrase, *4. Join* | phone app *6. TCP send* (blank host = gateway) to `.\m0.ps1 sink` |
| S2b M → P | Internet Sharing by hand (System Settings → General → Sharing), sharing from an unplugged port to Wi‑Fi | phone app *4. Join* | as S2a with `tcp_speed.py sink` |
| S4 P ↔ T | one phone app *3. Host* | the other app *4. Join* | *5. TCP sink* on the host, *6. TCP send* on the joiner |
| Bluetooth | phone app *7. advertise* or `open -n mac/otble.app --args serve` | phone app *8. find* or `--args connect` | printed by both |

Every permission and every OS dialog (Android's *Connect*, Bluetooth, Location) is answered
by the person running the test.
