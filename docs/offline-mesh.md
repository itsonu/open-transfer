# Nearby sharing and the device mesh (design, V2)

Status: **design, not built.** Nothing here ships until Phase M0 (below) shows each
piece works on real hardware.

## Goal

Open Transfer works like **Quick Share or AirDrop**: any nearby device shows up and
can be sent to from the share menu, whether or not it's on the same Wi‑Fi. And your
own devices form **one connected mesh**, the way Echo devices do: each one reaches
the others over whatever link it has (the same Wi‑Fi, Bluetooth, a phone's local
hotspot), and passes things on for a device the sender can't reach itself.

Three ways in, one network:

| Way in | Who | Exists today |
|---|---|---|
| Same Wi‑Fi (discovery) | anyone nearby, asked first | ✅ |
| Pairing code / QR link | your devices: trusted, then introduced to the rest | ✅ |
| Bluetooth nearby | your devices, or anyone while you're visible to everyone | new (this design) |

Paired devices find each other and send files, text, links and clipboard **even when
they are not on the same Wi‑Fi and there is no internet**: on a train, in a field, on
two different networks. Everything stays local: no relay server, no cloud, no internet.

## Required connection scenarios (product requirement)

Any supported combination of phones and computers connects and transfers over a local
network path, **with no internet and no shared router**. These four are requirements,
not experiments:

| # | Scenario | Host | Joins |
|---|---|---|---|
| S1 | Phone hosts a hotspot → laptop joins | Android phone | Mac, Windows, Linux laptop |
| S2 | Laptop hosts a hotspot → phone joins | Windows, Linux, Mac laptop | Android phone (also any browser phone) |
| S3 | Both on the room's Wi‑Fi | the router | everyone (✅ today) |
| S4 | Phone A hosts → phone B joins, both running Open Transfer | Android phone | Android phone or tablet |

The **connection manager** (below) detects which paths exist, picks the best usable
one, and takes the host or the joining role wherever the platform allows each. Where a
platform forbids a role, the manager uses the other device in that role, and the app
says so. Where no device can take it (see the restrictions table), the scenario
falls back to the user switching the hotspot on by hand, which the app then uses as
in S3. Each restriction below is either **verified** (with the evidence) or marked
**must verify in M0**; nothing is assumed.

The model is bitchat: devices talk over **Bluetooth LE**, and messages can hop through
other devices in range. Bluetooth is slow, so for anything bigger than a message the
devices use Bluetooth only to **arrange a fast link**: one phone opens a private,
local-only Wi‑Fi hotspot and the other joins it for the transfer. This is how Nearby
Share and AirDrop work.

## Not in scope, and why

| Asked for | Why not |
|---|---|
| WebRTC in the apps | WebRTC needs an IP route between the devices. With no shared network and no internet there is none, and STUN/TURN are internet servers. It would also add `aiortc`, a native library with no Android build (the same trap as cryptography, see [v1.md](v1.md)). The hotspot link below gives the same speed with the transfer code that already exists. |
| Share links to browsers over the mesh | A browser can't use Bluetooth mesh (Web Bluetooth can't advertise, and iPhones have none). Instead a phone can show a **Wi‑Fi QR code** for its local hotspot: a browser joins it, then opens the usual link (Phase M6). |
| iPhone / iPad apps | There's no native iOS app; the browser can't do Bluetooth. |
| Long-range or internet relays | Out of scope by choice: all local. |

## What already exists and is reused

- **Pairing** (SRP over the 6-digit code) leaves each pair with a 32-byte key; the
  **sync network** introduces paired devices to each other, and revocation reaches
  every member ([protocol.md](protocol.md#3-pairing), [sync.md](sync.md#trust-model)).
- **`ot-file/2`**: per-transfer forward-secret encryption over HTTP, for paired and
  unpaired apps ([encryption.md](encryption.md)).
- **Hotspots (H7)**: an app on a hotspot, or sharing one, already announces and
  serves itself on that address; apps sweep the /24 where multicast is dropped.
- **`sync/1` envelopes**: small signed and encrypted messages between paired devices
  (clipboard, introductions, revocations), already queued for offline members.

So the new parts are a **Bluetooth link**, a **mesh layer** over it, and the **hotspot
handoff**. Once two devices share the hotspot, the rest is today's code.

## Hotspot roles per platform: restrictions (verified 2026-11-12)

Checked against the SDKs on the development Mac (macOS 27.0.1 SDK headers; Android
`android-34`…`android-37.0` platform jars and sources) and on this Mac's own Wi‑Fi.
Windows could not be checked: the test laptop was not on the network.

| Platform | Host a hotspot from the app | Join a given network from the app | Evidence / restriction |
|---|---|---|---|
| **Android 8–15** | ✅ `WifiManager.startLocalOnlyHotspot`: no internet, **random SSID and passphrase** chosen by Android, read back from the reservation | ✅ `WifiNetworkSpecifier` + `ConnectivityManager.requestNetwork`: **Android always shows its own Connect dialog**; the network belongs to the app, which must bind to it (`bindProcessToNetwork`) | **Verified in the SDK.** Hosting needs `CHANGE_WIFI_STATE` + `NEARBY_WIFI_DEVICES` (Android 13+) or `ACCESS_FINE_LOCATION` (12 and below). The hotspot fails with `ERROR_INCOMPATIBLE_MODE` / `ERROR_TETHERING_DISALLOWED` when the user's own Mobile hotspot is on or tethering is blocked; the user can stop it in Settings; it ends when the app releases it. On phones without `isStaApConcurrencySupported()` the phone **leaves its Wi‑Fi** while hosting; on phones without `isStaConcurrencyForLocalOnlyConnectionsSupported()` it **leaves its Wi‑Fi** while joining. |
| **Android 16+** | ✅ as above, plus `startLocalOnlyHotspotWithConfiguration` (app picks band etc.) | ✅ as above | **Verified in the SDK:** new in API 36. `TetheringManager.startTethering` (the user's normal hotspot) is public in API 36 but needs `TETHER_PRIVILEGED`, which apps can't hold: **the app can't switch on the normal Mobile hotspot.** |
| **Android 9 and older** (phone 2) | — | — | The app needs Android 10 (`minSdk 29`). Such a phone joins a hotspot by hand and uses the browser. |
| **macOS** | ❌ **No public API.** CoreWLAN only has `startIBSSMode…` (ad-hoc, WEP only, deprecated since macOS 11; Android can't join ad-hoc networks). Internet Sharing has no API and needs an admin and a source interface | ⚠️ `CWInterface.associate(to:password:)` needs a `CWNetwork` from a scan, and **a scan returns no network names without Location permission** | **Verified:** `CWInterface.h` in the macOS 27 SDK; a scan on this Mac without Location permission found 4 networks, 0 with names, and couldn't read the current network's name. **Must verify in M0:** `networksetup -setairportnetwork en0 <ssid> <password>` without Location permission; Internet Sharing (Wi‑Fi) with no upstream connection, by hand. |
| **Windows 10/11** | ⚠️ (a) Wi‑Fi Direct *legacy* access point (`WiFiDirectAdvertisementPublisher` with `LegacySettings`: SSID + passphrase, phones join it as a normal Wi‑Fi); (b) Mobile Hotspot (`NetworkOperatorTetheringManager`), which may need an upstream connection | ⚠️ `netsh wlan add profile` (per user) + `netsh wlan connect`, or the WLAN API | **Must verify in M0** on the Windows laptop: which of (a)/(b) starts with no internet and no router; whether joining needs admin. `netsh wlan … hostednetwork` is deprecated and most current drivers report *Hosted network supported: No* (check `netsh wlan show drivers`). |
| **Linux** | ✅ NetworkManager (`nmcli device wifi hotspot`) | ✅ `nmcli device wifi connect` | Not a target device for M0; follows after. |
| **iPhone / iPad (browser)** | ❌ (Personal Hotspot by hand only) | by hand; the Camera app joins a Wi‑Fi QR code | Browser only, as today. |

**What this means for the four scenarios:**

| | Android phone | Samsung tablet | Mac | Windows laptop |
|---|---|---|---|---|
| Can host from the app | ✅ (verify on device) | ✅ (verify on device) | ❌ (by hand: Internet Sharing, must verify) | ⚠️ must verify |
| Can join from the app | ✅ with Android's dialog | ✅ with Android's dialog | ⚠️ needs Location permission, or `networksetup` (verify) | ⚠️ must verify |

- **S1 (phone hosts → laptop joins):** expected to work for Mac and Windows; the laptop
  joining by itself depends on the M0 checks, otherwise the user picks the network from
  the Wi‑Fi menu once (name and password shown on the phone, or the Wi‑Fi QR).
- **S2 (laptop hosts → phone joins):** **Windows** if (a) or (b) works without
  internet. **Mac: the app can't host.** The only way is Internet Sharing by hand, and
  only if M0 shows it runs with no upstream; if it doesn't, S2 is **not possible with a
  Mac as host** and the manager uses S1 instead (the phone hosts) and says why.
- **S3:** ✅ today, verified on real hardware.
- **S4 (phone hosts → phone joins):** LocalOnlyHotspot + `WifiNetworkSpecifier`, both
  Android; the joining phone shows Android's Connect dialog once per handoff.

## Bluetooth roles per platform

| | Android app | Mac app | Windows app | Linux | Browser / iPhone |
|---|---|---|---|---|---|
| BLE scan (central) | ✅ Kotlin | ✅ CoreBluetooth | ⚠️ WinRT | ⚠️ BlueZ | ❌ for this |
| BLE advertise + GATT server (peripheral) | ⚠️ most phones (`isMultipleAdvertisementSupported`) | ✅ **verified** (M0, run as its own app) | ⚠️ WinRT, only on adapters that report `IsPeripheralRoleSupported` | ⚠️ BlueZ D-Bus | ❌ |
| Runs in the background | ✅ foreground service (`connectedDevice`) | ✅ menu-bar app | ✅ tray app | ✅ | — |
| Permissions the user grants | Nearby devices (Bluetooth), Nearby Wi‑Fi devices | Bluetooth (per app: the app's Info.plist needs `NSBluetoothAlwaysUsageDescription`); Location (to join Wi‑Fi by name) | none special | none special | — |

⚠️ = must be proven on the real devices in M0. The user grants every permission
themselves; the app never changes system or security settings.

- **Mac ↔ Mac** with no phone or Windows laptop: no device can host from the app, so
  Bluetooth only (small items), or Internet Sharing by hand if M0 shows it works.
- A device that **leaves its current Wi‑Fi** to join a hotspot (see the concurrency
  notes above) is told first, and rejoins it afterwards.

## Connection manager

One component on every app decides how two devices connect. It runs whenever a send
targets a device that isn't reachable on any current address.

```
1. Already connected?  The same Wi‑Fi (S3), or both already on some hotspot
   (one of them switched it on by hand): use it. Nothing to arrange.
2. Can the two arrange a hotspot?  Host = the first device in this order that can host
   from the app: Android (LocalOnlyHotspot) › Windows (if M0 passes) › Linux.
   The other must be able to join: from the app (Android, Windows, Linux, Mac with
   Location) or, failing that, by hand with the name and password shown.
3. Credentials: host → joiner over
     a. Bluetooth, sealed under the pair key (once M1/M2 exist), or
     b. a QR code on the host's screen: `WIFI:T:WPA;S:<ssid>;P:<pass>;;` plus the
        Open Transfer link, scanned by the joiner's app (or any phone's camera), or
     c. the name and password shown, typed by hand.
4. Find each other on the hotspot: the joiner connects to **its default gateway**
   (on a hotspot that is the host) on the app port; multicast and the /24 sweep (H7)
   as usual. The host never needs to know its own hotspot address (Android apps can't
   list interfaces; see the 192.0.0.4 fix).
5. Transfer: the usual signed offer + `ot-file/2`. The hotspot adds no trust.
6. Tear down: the host releases the hotspot; the joiner forgets the network and
   rejoins its previous Wi‑Fi. On failure the send ends with the reason
   (*declined in Android's dialog*, *Location is off*, *hotspot blocked by the user's
   Mobile hotspot*…).
```

Paths are ranked by speed and by how much they disturb the user: existing Wi‑Fi >
an existing hotspot > an arranged hotspot where nobody leaves a network > one where the
joiner leaves its Wi‑Fi > Bluetooth (small items only).

## Architecture

```
            ┌──────────────── existing ────────────────┐
 page/UI ── │ jobs, offers, ot-file/2, sync/1, history │
            └───────────────┬──────────────────────────┘
                            │ "send this to device X"
                     ┌──────┴──────┐
                     │  Link picker │  LAN first; else BLE (small) or hotspot handoff (big)
                     └──┬────┬───┬─┘
              LAN (HTTP)│    │   │Hotspot handoff → then LAN (HTTP) on the hotspot
                        │  BLE mesh
                        │    │
            ┌───────────┴────┴───────────────────┐
            │ Native Bluetooth per platform       │  Kotlin · CoreBluetooth · WinRT · BlueZ
            │ (advertise, scan, GATT, framing)    │
            └─────────────────────────────────────┘
```

- **Native layer** (per platform) does only radio work: advertise, scan, a GATT
  service with one write and one notify characteristic, MTU negotiation, framing.
  It hands whole frames to the Python app (Android: through the existing bridge;
  desktop: an in-process binding) and knows nothing about keys.
- **Mesh layer** (Python, shared by all platforms): envelopes, hops, dedupe, routing,
  rate limits, encryption. Tested with an in-memory fake radio.
- **Link picker**: today's LAN path is always tried first. Only when a paired device
  is unreachable on every LAN address does a send go over Bluetooth.

## Quick Share-style sharing

How Quick Share and AirDrop behave, and what Open Transfer does the same way:

- **Visibility**, one setting on every device (as Quick Share's):
  **Off** · **Your devices** (default: only paired devices can see it) ·
  **Everyone for 10 minutes** (anyone nearby; it switches back by itself).
- **The sender's list** shows everyone it can reach on any link: your devices first,
  then visible strangers. A tile says how it's reached (*Wi‑Fi*, *Bluetooth*, *via Studio
  Mac*). Tapping a stranger sends an offer they Accept or Decline, as on Wi‑Fi today;
  your devices accept automatically.
- **The fastest link is picked for you:** same Wi‑Fi if there is one; otherwise
  Bluetooth arranges a local hotspot for big files (below); small items go over
  Bluetooth directly.
- **Share from anywhere:**

  | Platform | Entry point | Status / how |
  |---|---|---|
  | Android | share sheet (photos, files, text, links) | ✅ exists |
  | Android | Quick Settings tile: *Visible to everyone for 10 min* | new; next to the existing *Send clipboard* tile |
  | Mac | **Share → Open Transfer** in Finder and apps | a Share extension needs the signed, notarized app (on hold); a **Services** menu entry (`NSServices` in the app's Info.plist) needs no extension and comes first |
  | Mac | right-click → Quick Actions | from the same Services entry |
  | Windows | right-click → **Send to → Open Transfer** | a shortcut in the user's SendTo folder, made by the installer; no packaging needed |
  | Windows | the system Share window | needs an MSIX package; later |
  | Browsers | the page's own picker and drop zone | ✅ exists |

- **Not the same as Google's Quick Share or Apple's AirDrop:** those protocols are
  private and change without notice. Open Transfer talks to Open Transfer only. (An
  unofficial Quick Share receiver for the Mac exists in the open-source NearDrop; see
  the open questions.)

## One mesh across every link (Echo-style)

Each app keeps a list of **neighbours on every link**: Wi‑Fi (today's discovery),
Bluetooth, and a hotspot it hosts or joined. Your devices then work as one network:

- **Reachability in hellos:** every member says which members it can reach directly
  and over which link (`{device tag, link, quality}`), refreshed every 30 s, sealed for
  members only. That gives each member a small map of the network.
- **Routing:** a send to device X goes over the best direct link (Wi‑Fi first). If X
  isn't directly reachable, it goes **through a member that can reach X**, with the
  fewest hops, preferring Wi‑Fi hops. Example: the phone is out of the Mac's Bluetooth
  range and on mobile data; the tablet is on the Mac's Wi‑Fi and near the phone. Mac →
  (Wi‑Fi) → tablet → (Bluetooth) → phone.
- **End to end:** a bridge never opens what it forwards. Small items are sync/1
  envelopes sealed by the source for the destination (sync/1 already forwards this
  way); files are `ot-file/2` from source to destination, and a bridge only passes the
  encrypted bytes on.
- **What can cross a bridge:** small items over any hop. Files only over hops that are
  all fast (Wi‑Fi or a hotspot); a file never crawls over a Bluetooth hop, it waits for
  a fast route or a handoff instead.
- **A home hub:** any always-on member (a desktop at home, a spare phone on a charger)
  keeps queues for devices that are away and bridges Bluetooth-only devices onto the
  Wi‑Fi, as an Echo does for nearby gadgets. Nothing special: every member can do it,
  an always-on one just does it more.
- **Membership is unchanged:** pairing (code, QR link) and introductions decide who's
  in; revocation removes a device from every link at once.

## Bluetooth link

- **Service:** one 128-bit Open Transfer service UUID; a `rx` characteristic
  (write without response) and a `tx` characteristic (notify).
- **Advertisement, private by default:** no device id or name in the clear. Each
  device advertises a rotating 8-byte **tag** = `HMAC(K_pair, "ot-ble-tag" ‖ epoch)`
  per paired device, truncated, epoch = 15 minutes (the same idea as Apple's
  resolvable private addresses). A paired device that scans recognises the tag; a
  stranger sees random bytes that change every 15 minutes. With many pairs the tags
  go in the scan response or are fetched on connect.
- **"Visible to everyone nearby"** is only on while *Add device* or *Send to someone
  nearby* is open, as LAN pairing is today. Then the advertisement also carries a
  fixed "open-transfer-nearby" flag and the device's display name.
- **Framing:** frames of up to MTU − 3 bytes (MTU negotiated up to 517); a frame header
  of message id, index, count; a window of 8 unacknowledged frames; a whole message is
  capped at **64 KiB**. Bigger content goes by hotspot handoff.
- **Throughput budget** (to measure in M0): expect roughly 10–100 KB/s per hop. A
  64 KiB message is seconds on one hop; a 5 MB photo would be minutes, which is why
  files use the hotspot.

## Mesh layer (bitchat-style)

**Envelope** (CBOR), sent over one hop and possibly relayed:

```
v        1
id       16 random bytes            dedupe; replays refused
ttl      hops left (start 4, max 7)
to       routing tag for the destination (rotating, as in the advertisement)
from_tag the sender's rotating tag (lets the receiver find the pair key)
kind     "sync" | "offer" | "handoff" | "ack"
body     sealed with the sync/1 seal (HKDF from the pair key, HMAC-SHA256 counter-mode
         keystream, encrypt-then-MAC), the MAC also covering id, to and kind
```

- **Relaying:** a device that isn't the destination decrements `ttl`, remembers `id`
  (LRU of 4096 ids, 10 minutes) and passes it to its other Bluetooth neighbours. By
  default devices relay **only within their own network** (envelopes whose `to` tag
  belongs to a member they know). *Help other people's messages along* is an opt-in
  setting, off by default.
- **Relays see** only: that some Open Transfer message of some size is going by, its
  TTL and a tag that rotates. Not who, not what.
- **Store and forward:** an envelope for a paired device that isn't in range waits in
  the existing sync/1 queue for as long as its kind allows there (clips 120 s by
  default, introductions and revocations up to 7 days), then goes when that device
  appears on any link.
- **Limits:** 30 envelopes per minute per neighbour; anything over 64 KiB or with a
  bad seal is dropped and counted, never relayed.

**What goes over the mesh:** clipboard (sync/1), text and links, files up to 64 KiB,
introductions and revocations, and the **handoff** messages below. Everything else
waits for a fast link.

## Hotspot handoff (big files, one hop)

Both devices must be in Bluetooth range of each other (not relayed): the hotspot only
reaches as far as Wi‑Fi does anyway.

```
1. Sender → receiver (BLE, sealed):  handoff-request {job, total size, file count}
2. The device that can host (Android preferred; the receiver if both can) starts a
   LocalOnlyHotspot. Android picks the SSID and a random WPA2 passphrase each time.
3. Host → other (BLE, sealed):       handoff-offer {ssid, passphrase, host ip, port,
                                      expires in 120 s}
4. The other device joins (Android: the system's Connect dialog; Mac: CoreWLAN;
   Windows: a temporary WLAN profile), saying first that it leaves its current Wi‑Fi.
5. Both apps already listen on the new interface (H7). The sender runs the usual
   signed offer + ot-file/2 transfer over HTTP to host ip:port.
6. Done, failed or 10 minutes idle: the host stops the hotspot; the joiner forgets
   the network and rejoins its previous Wi‑Fi.
```

- The hotspot credentials only ever travel sealed under the pair key, and are new
  each time. The transfer is still `ot-file/2`, so the hotspot adds no trust.
- **Unpaired** handoff (M6): both sides use `ot-file/2`'s unpaired mode, and the
  receiver accepts first, exactly as an unpaired LAN offer.
- If joining fails (the user declines the dialog, the permission is off), the send
  ends with that reason. Files up to 64 KiB still go over Bluetooth.

## Security and privacy

| Threat | Answer |
|---|---|
| Tracking a phone by its Bluetooth advertisement | Rotating tags, no ids or names in the clear except while *Visible to everyone* is open. The OS rotates the Bluetooth MAC address. |
| Someone pretending to be a paired device | Tags and envelopes need the pair key; the seal's MAC covers the header too; ids are remembered, so replays are refused. |
| A relay reading or changing messages | It holds no key: it sees ciphertext; a change fails the seal. |
| Flooding the mesh | Per-neighbour rate limits, TTL ≤ 7, 64 KiB cap, relaying only for its own network by default. |
| Hotspot credentials stolen over the air | Sent sealed; random per handoff; valid 120 s; the transfer on top is `ot-file/2` anyway. |
| A revoked device keeps relaying or advertising | Its key is gone everywhere: its tags aren't recognised and its envelopes don't open. |
| Downgrade from the LAN | The link picker never prefers Bluetooth when the LAN works; encryption rules are the same on every link. |

Security review (as for `ot-file/2` in [encryption.md](encryption.md#review-nonces-keys-replay-metadata))
is a gate before M2 ships.

## Phases

Each phase: tests first, then the code, the whole suite and CI, then a real-device
run. Emulators have no usable Bluetooth or Wi‑Fi hosting, so **every phase needs a
person with the devices** for its last check; the automated part runs the mesh logic
against an in-memory fake radio.

| # | Phase | Done when (automated) | Done when (real devices) |
|---|---|---|---|
| M0 | **Spike, go / no-go.** Throwaway native snippets on each platform: advertise + scan + GATT echo; measured throughput Android ↔ Mac ↔ Windows; LocalOnlyHotspot + join from Mac, Windows, Android; Windows hosting without upstream | — | A table of what works on your Android phone, the Samsung tablet, the Mac and the Windows laptop, with numbers. Anything ❌ re-plans the phases |
| M1 | **Find paired devices by Bluetooth** (Android + Mac). Tiles say *Nearby · Bluetooth* | Tag rotation and recognition; fake-radio discovery between two in-process apps | Phone and Mac with Wi‑Fi off see each other's tiles within 10 s; a stranger's scan shows no ids |
| M2 | **Send small items over Bluetooth** (one hop): clipboard, text, links, files ≤ 64 KiB | Framing, windowing, sealing, replay, size cap; history and Activity as for LAN sends | Clipboard and a 50 KB file both ways with Wi‑Fi off; a revoked device can't send |
| M3 | **Connection manager + hotspot handoff:** S1, S2, S4 arranged by the app (credentials by QR first, Bluetooth once M2 exists) | Path ranking; handoff state machine with fakes (timeouts, declined dialog, no Location, hotspot refused, rejoin) | The M0 matrix's S1, S2 and S4 rows, now driven by the app: 1 GB each way, no router, no internet; every joiner back on its Wi‑Fi afterwards |
| M4 | **Windows** Bluetooth and joining | Same tests on Windows CI with the fake radio | Windows ↔ phone: small items over Bluetooth, 1 GB by handoff |
| M5 | **One mesh across links:** reachability in hellos, routing through members, bridging Wi‑Fi ↔ Bluetooth, multi-hop for small items | Route choice, dedupe, TTL, rate limits, store-and-forward on a fake 5-device network mixing links | Mac → (Wi‑Fi) → tablet → (Bluetooth) → phone with the phone off the Mac's Wi‑Fi and out of its Bluetooth range |
| M6 | **Visible to everyone for 10 minutes:** unpaired nearby share over Bluetooth, and a **Wi‑Fi QR** so a browser (iPhone) can join the phone's hotspot and use the link | Unpaired handoff asks the receiver first; visibility switches back after 10 min; Wi‑Fi QR format | A stranger's phone receives a photo after accepting; an iPhone joins by the Wi‑Fi QR and downloads |
| M7 | **Share from anywhere:** Mac Services menu, Windows *Send to*, Android Quick Settings tile | Installer creates the SendTo shortcut; the Services entry hands files to the app | Right-click a file → Open Transfer → it's staged, on each computer |

### M0 test matrix: the four scenarios on the target devices

Devices: **P** = Android phone (Nirakara), **T** = Samsung tablet, **M** = MacBook Air
(macOS 27), **W** = Windows 11 laptop, **P2** = Android 9 phone (browser only). Router
off or out of reach and **no internet** in every row except S3.

Each row is run twice: **by hand** first (the user switches the hotspot on and joins
from the system's Wi‑Fi menu; this needs no new code, because H7 already serves on a
hotspot) and then **by the spike tools** for the from-the-app roles. Record pass/fail,
the exact OS dialog or error, and transfer speed (1 GB).

| # | Host → joiner | Host role (how) | Join role (how) | Pass when |
|---|---|---|---|---|
| S1a | P → M | LocalOnlyHotspot (spike app) | `networksetup -setairportnetwork` without Location; then CoreWLAN with Location granted | Both see each other's tile; 1 GB both ways; M rejoins its Wi‑Fi after |
| S1b | P → W | LocalOnlyHotspot | `netsh wlan add profile` + `connect`, as a normal user | Same |
| S1c | T → M | LocalOnlyHotspot on the tablet | as S1a | Same |
| S2a | W → P | Wi‑Fi Direct legacy AP; else Mobile Hotspot | `WifiNetworkSpecifier` (Android's dialog) | Same; record which Windows option worked with no upstream |
| S2b | M → P | Internet Sharing by hand, sharing from an unplugged interface | `WifiNetworkSpecifier` | Same, **or** a documented ❌ for Mac hosting (then S2 with a Mac = not possible) |
| S2c | W → P2 | as S2a | P2 joins by hand, uses the browser | P2 sends and receives in the browser |
| S3 | router → all | — | — | ✅ already verified on real hardware (Mac, Windows, Android, 2026-11-07) |
| S4a | P → T | LocalOnlyHotspot | `WifiNetworkSpecifier` | Same; record whether each device left its own Wi‑Fi (`isStaApConcurrencySupported`, `isStaConcurrencyForLocalOnlyConnectionsSupported`) |
| S4b | T → P | LocalOnlyHotspot on the tablet | `WifiNetworkSpecifier` | Same |
| X1 | any host | — | — | The hosting phone's app is reachable at the joiner's **default gateway** address |
| X2 | P (its Mobile hotspot on by hand) | user's hotspot | the app tries LocalOnlyHotspot | Fails cleanly with `ERROR_INCOMPATIBLE_MODE` and the app uses the hotspot that's already on |

**Go / no-go:** S1, S2 (with Windows) and S4 must pass to start M3. A Mac that can't
host is an accepted platform restriction (S1 covers Mac + phone); anything else that
fails re-plans the phases before code is written.

### M0 results so far

| Check | Result |
|---|---|
| Mac advertises a GATT service (`spikes/m0/mac/otble.swift`) | ✅ Bluetooth on, *advertising OT-M0*, when run as its own app (`otble.app`). Started from a shell instead, macOS stops it (exit 134): Bluetooth permission belongs to the app that launched it. **The Mac app needs `NSBluetoothAlwaysUsageDescription` in its Info.plist.** |
| Mac hosting a hotspot from an app | ❌ **verified: no public API** (macOS 27 SDK `CWInterface.h`: only deprecated ad-hoc IBSS) |
| Mac joining by network name without Location permission | ❌ **verified:** a CoreWLAN scan found 4 networks, 0 with names; current network name unreadable. `networksetup` path still to check |
| Android hosting / joining APIs | ✅ **verified in the SDK** (API 26+ hosting, API 29+ joining with Android's dialog; API 36 adds configured hosting; normal Mobile hotspot not startable by apps) |
| The rest of the matrix (S1, S2, S4, X1, X2), Bluetooth throughput | not run: needs the phone, tablet and Windows laptop on hand |

**New dependencies** (each needs your OK before it is added): on macOS
`pyobjc-framework-CoreBluetooth` and `pyobjc-framework-CoreWLAN`; on Windows the
`winrt` Bluetooth and WLAN packages; Android needs none (platform APIs).

## Open questions for you

1. **M0 hardware:** which devices can you run the spike on, and is the Samsung tablet
   available? M0 decides everything after it.
2. **Leaving Wi‑Fi for a handoff:** acceptable on a laptop that's on a work network?
   The app will always say so and ask first.
3. **Relaying for strangers:** keep *Help other people's messages along* off by
   default, as above?
4. **Talking to Google's Quick Share:** the Mac could also *receive* from any Android
   phone's built-in Quick Share (as the open-source NearDrop does), with no Open
   Transfer on the phone. It's an unofficial, reverse-engineered protocol Google can
   break at any time. Worth a separate experiment, or leave it out?
