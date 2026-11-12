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

## Platform capabilities (to confirm in M0)

| | Android app | Mac app | Windows app | Linux | Browser / iPhone |
|---|---|---|---|---|---|
| BLE scan (central) | ✅ Kotlin | ✅ CoreBluetooth | ✅ WinRT | ✅ BlueZ | ❌ for this |
| BLE advertise + GATT server (peripheral) | ✅ most phones | ✅ CoreBluetooth | ⚠️ WinRT, only on adapters that support the role | ⚠️ BlueZ D-Bus | ❌ |
| Create a local-only hotspot | ✅ `LocalOnlyHotspot` (Android 8+) | ❌ (Internet Sharing needs the user and an upstream) | ⚠️ Mobile Hotspot API, may need an upstream connection | ⚠️ NetworkManager | ❌ |
| Join a given Wi‑Fi from the app | ⚠️ `WifiNetworkSpecifier`: Android shows its own **Connect** dialog every time | ⚠️ CoreWLAN, needs Location permission | ✅ WLAN API with a temporary profile | ✅ NetworkManager | ❌ |
| Runs in the background | ✅ foreground service (`connectedDevice`) | ✅ menu-bar app | ✅ tray app | ✅ | — |
| Permissions the user grants | Nearby devices (Bluetooth), Nearby Wi‑Fi devices | Bluetooth; Location (to join Wi‑Fi) | none special | none special | — |

⚠️ = expected to work but must be proven on the real devices in M0. The user grants
every permission themselves; the app never changes system or security settings.

**Consequences:**

- Any pair that includes an **Android phone** gets the fast link (the phone hosts).
- **Mac ↔ Mac** or **Mac ↔ Windows** with no phone: Bluetooth only (small items),
  unless M0 shows Windows can host a hotspot with no upstream connection.
- The device joining the hotspot **leaves its current Wi‑Fi** for the transfer; the
  app rejoins it afterwards and says so before it starts.

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
| M3 | **Hotspot handoff** for big files, Android hosting; Mac and Android joining | Handoff state machine with fakes (timeouts, declined dialog, rejoin) | 1 GB phone → Mac and Mac → phone with no shared Wi‑Fi; the Mac rejoins its Wi‑Fi afterwards |
| M4 | **Windows** Bluetooth and joining | Same tests on Windows CI with the fake radio | Windows ↔ phone: small items over Bluetooth, 1 GB by handoff |
| M5 | **One mesh across links:** reachability in hellos, routing through members, bridging Wi‑Fi ↔ Bluetooth, multi-hop for small items | Route choice, dedupe, TTL, rate limits, store-and-forward on a fake 5-device network mixing links | Mac → (Wi‑Fi) → tablet → (Bluetooth) → phone with the phone off the Mac's Wi‑Fi and out of its Bluetooth range |
| M6 | **Visible to everyone for 10 minutes:** unpaired nearby share over Bluetooth, and a **Wi‑Fi QR** so a browser (iPhone) can join the phone's hotspot and use the link | Unpaired handoff asks the receiver first; visibility switches back after 10 min; Wi‑Fi QR format | A stranger's phone receives a photo after accepting; an iPhone joins by the Wi‑Fi QR and downloads |
| M7 | **Share from anywhere:** Mac Services menu, Windows *Send to*, Android Quick Settings tile | Installer creates the SendTo shortcut; the Services entry hands files to the app | Right-click a file → Open Transfer → it's staged, on each computer |

### M0 results so far

| Check | Result |
|---|---|
| Mac advertises a GATT service (`spikes/m0/mac/otble.swift`) | ✅ Bluetooth on, *advertising OT-M0*, when run as its own app (`otble.app`). Started from a shell instead, macOS stops it (exit 134): Bluetooth permission belongs to the app that launched it. **The Mac app needs `NSBluetoothAlwaysUsageDescription` in its Info.plist.** |
| Mac ↔ Android, Mac ↔ Windows throughput | not run yet: needs the Android spike app and the devices on hand |
| Android LocalOnlyHotspot; Mac and Windows joining it | not run yet |

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
