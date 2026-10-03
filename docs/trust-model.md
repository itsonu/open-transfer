# Trust model

What pairing in Open Transfer guarantees, how it works, and what it does not
protect against. This document is normative: the code in
`src/open_transfer/srp.py`, `devices.py` and `mesh.py` implements it, and
`tests/test_pairing.py` tests it.

## Who is who

| Term | What it is | Where it lives |
|---|---|---|
| **Device identity** | a random id (`d-` + 24 hex chars) and a display name | `<state>/device.json` |
| **Pair key** | a 256-bit secret shared by exactly two paired devices, derived during pairing | `<state>/trusted.json` (owner-only file) on both devices |
| **Pairing code** | 6 digits shown on one device while its *Add device* screen is open | memory only |
| **Pairing session** | one attempt to pair: SRP ephemerals, salt, a session id | memory only, 2 minutes at most |

There is no global identity and no certificate authority. A device's id is
just a name; what makes a device *trusted* is that it can prove it holds the
pair key we agreed with it during pairing.

## Threat model

* **In scope:** anyone on the same network who can watch all traffic
  (multicast, HTTP), replay or alter it, send their own packets, and run their
  own Open Transfer devices.
* **Out of scope:** someone who controls one of the paired devices, or can read
  its state folder; reading file *contents* on the wire (v3 uses plain HTTP on
  the LAN, so files and names are visible to anyone who can watch traffic);
  denial of service by flooding the network.

## Pairing

Pairing uses **SRP-6a** (RFC 5054, 2048-bit group, SHA-256), a
password-authenticated key exchange, with the 6-digit code as the password.
SRP is used because the code is short: a passive observer of a pairing learns
nothing that lets them test guesses offline, and an active attacker gets
**one guess per attempt**, and attempts are rate-limited. Hashing or HMAC-ing a
6-digit code (what v3.0 betas did) is *not* safe: 10⁶ guesses take
milliseconds.

```
 Shows the code (A)                                    Enters / scans it (B)
 Add device open → pairing window, code C
                     ←── POST /p2p/pair/begin {device B}
 salt s, b; v = g^x(s, ids, C); B_pub = k·v + g^b
                     ──► {session, s, B_pub, device A}
                                                     a; A_pub = g^a; K = H(S)
                     ←── POST /p2p/pair/prove {session, A_pub, M1}
 checks M1 (wrong → "code isn't right", 1 strike)
 asks its owner: "Pair with ‹B›?"  Allow / Deny
                     ──► {M2}                        checks M2: A really knew C
                     ←── POST /p2p/pair/status {session, mac}  (repeated)
 allowed → stores pair key
                     ──► {status: allowed, mac}       stores pair key
```

* The identity string bound into SRP is `idA|idB`, so the key belongs to these
  two ids and nobody else.
* Pair key = HMAC-SHA256(K, `open-transfer-pair-key/2|idA|idB`).
* `status` requests and answers carry HMAC(K, …) so nobody without K can fake
  "allowed".
* B's user started the pairing by entering or scanning the code (that is B's
  confirmation). A's user must press **Allow**. Pairing is never established
  just because a code matched.

### The pairing window

A device accepts its code **only while its *Add device* screen is open** (the
page renews the window every 20 s; it closes 30 s after the last renewal, or
right away when the screen closes). Outside the window, there is nothing to
guess. The code also changes every 10 minutes, after each successful pairing,
and after 10 wrong guesses; 5 wrong attempts from one address in a minute are
refused (`rate_limited`).

### Finding the other device

Entering a code **never depends on multicast**:

1. If the user picked the device (tile → *Pair*, or a scanned QR naming it),
   only that device is asked.
2. Otherwise every reachable device that says its pairing window is open
   (`pairing: true` in its hello/info answer) is asked over HTTP, plus any
   device that answers a multicast `find`. `find` carries **nothing derived from
   the code**; answers are sent straight back (unicast) as well as multicast.
3. SRP then runs against the candidates; at most one can know the code.

Errors say what actually happened:

| `reason_code` | Meaning |
|---|---|
| `discovery_unavailable` | no device could be found to ask, and multicast isn't working here; use the QR code or the address shown on the other device |
| `pairing_not_open` | devices were found, but none has *Add device* open |
| `pairing_code_invalid` | the device showing a code says this code isn't it |
| `pairing_expired` | the chosen device's pairing window closed |
| `trust_rejected` | its owner pressed *Deny*, or didn't answer within 60 s |
| `device_unreachable` | the device is known but can't be reached over HTTP |
| `rate_limited` | too many wrong codes; try again after `retry_after` seconds |
| `already_paired` | both sides already trust each other |
| `identity_mismatch` | the device at that address isn't the one that was chosen |

## After pairing: authenticated messages

| Message | Authentication |
|---|---|
| requests from a paired device (hello, offers, unpair) | `X-OT-From` + `X-OT-Auth` = timestamp + HMAC(pair key, method, path, sender, body hash); ±120 s window; each MAC accepted once |
| answers to those requests | `X-OT-Auth` = HMAC(pair key, "response", request MAC, status, body hash); a paired device's answer without it is rejected |
| offer status, upload, cancel, signals | the per-transfer secret given to the sender with the offer |
| multicast announce / reply / bye / find | **not authenticated** — treated only as hints (see below) |
| hello from an unpaired device | not authenticated (nothing to authenticate against); can't affect paired devices |

### Address changes

A paired device's address is only changed after it **proves** the pair key at
the new address:

```
known paired id ── seen at a new address (announce, unsigned hello) ──► challenge
challenge: signed POST /p2p/hello to the new address with a fresh nonce
verified:  the answer carries a valid response MAC ──► address updated
otherwise: the old address stays; the attempt is logged (address_verification_failed)
```

A signed hello from the new address is itself the proof. Multicast `bye` only
counts when it comes from the device's current address. So an attacker who
copies a paired device's id into their own announcements cannot redirect the
files meant for it, and cannot rename it.

## Decisions

1. **What survives an app restart?** Everything: device id, name, pair keys,
   history. (`device.json`, `trusted.json`, `history.db` in the state folder.)
2. **What survives a reinstall?** Nothing on Android (uninstall deletes app
   data; `allowBackup` is off on purpose, so keys never reach a cloud backup).
   A reinstalled app is a **new device** with a new id. On desktop the state
   folder survives a reinstall unless the user deletes it.
3. **Recovering a lost pairing database.** There is no recovery: pair again.
   The other devices notice: the next signed request from them is answered with
   `peer_unpaired`, and they drop their side too. Old entries for a reinstalled
   device show as "(old device)" and can be removed.
4. **A paired device changes IP.** It keeps its identity; the new address is
   used only after the challenge above succeeds. Until then it shows as
   *reconnecting* / *not nearby*.
5. **What unpairing invalidates.** The pair key, on both devices when the other
   is reachable (signed `DELETE /p2p/pair`), and on the other device the next
   time it contacts us otherwise (`peer_unpaired`). After unpairing, transfers
   need *Accept* again, and the old key is useless: we no longer have it.
   History is kept.
6. **Safe to send by multicast:** device id, name, form, platform, HTTP port,
   version, and whether the pairing window is open. Never anything derived
   from a code or a key.
7. **Safe to put in a QR code:** the address, the device id, and something
   short-lived — never the PIN or a key.
   * *Add device* (pairing) QR: the current pairing code; it works only while
     the window is open, for one pairing. A browser that scans it is admitted
     like the PIN would admit it, that browser only.
   * Share-link QR (`--share-folder`, the visitor's share sheet): when a PIN is
     set, a **join token** `<expiry>.<HMAC(secret key, expiry, PIN digest)>`
     valid for 10 minutes. A photo of the QR is useless afterwards, can't be
     extended or forged, and stops working at once if the PIN changes.
   * Terminal QR: just the address; the PIN is typed (it's printed next to it).
8. **What the 6-digit code authenticates.** That B is talking to the device
   whose screen the user is looking at (and A to the device whose user read the
   code), for one pairing attempt. Nothing else; it is never reused as a key.
9. **What needs explicit confirmation.** Every pairing: B by entering or
   scanning the code, A by pressing *Allow*. Unpairing doesn't (it removes
   trust). Transfers from unpaired devices need *Accept*.
10. **Who is right when pairing state disagrees.** Each device decides only
    whether *it* trusts the other, and trust is used only when both hold the
    same key (a signature has to verify on the receiving side). A one-sided
    entry therefore grants nothing and is cleaned up by `peer_unpaired`.

## Limitations

* File contents and names travel in plain HTTP on the LAN (encryption is a
  planned post-3.0 item). Pairing protects *who* you talk to, not secrecy.
* Unpaired devices have no verifiable identity: a name on an unpaired tile is
  just what that device says. Anything from an unpaired device needs *Accept*.
* Anything that runs on the same device can act as its owner through
  `127.0.0.1` (that's how the app's own window talks to it).
* A multicast `bye` spoofed with the device's own source address can make it
  look offline until its next hello (seconds).
* The code is 6 digits: an attacker on the LAN who keeps guessing while your
  *Add device* screen is open gets 5 tries a minute, at most 10 per code.
