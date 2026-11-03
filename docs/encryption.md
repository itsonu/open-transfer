# File transfer encryption (`ot-file/2`)

Files sent between two **paired** apps are encrypted end to end: only the
receiving app can read them, and any change, reordering, replay or
truncation is detected before a file is kept. Between **unpaired** apps the
same encryption runs on a one-off key, which stops a listener but not
someone who can pose as the other app. A browser's own hop to its app is
encrypted only with `--https`. This page covers the threat model, the
design, every path a file can take, and what is still in plain text.

## Threat model

| Who | Can | Defended |
|---|---|---|
| **Passive listener** on the Wi‑Fi or hotspot (another guest, a compromised router) | Read every packet | Paired app ↔ app: file contents and file names are unreadable. Sizes and timing are visible. |
| **Active attacker** on the LAN (ARP spoofing, rogue hotspot, a spoofed multicast packet) | Change, drop, replay or inject packets; strip headers | Changed or injected bytes are rejected per 64 KiB record. Replays into another file or transfer fail because each uses a different key. Truncation fails. A downgrade to plain text is refused (see below). Dropping packets can still stop a transfer, which is a denial of service, not a disclosure. |
| **A paired device** | Decrypt what was sent *to it* | Each recipient's key is its own pair key, so in a group send, B can't decrypt what went to C. |
| **An unpaired app** | Send offers (asked first), receive what you choose to send it | No shared key, so a one-off one: X25519 to the receiver's per-run key. A listener reads nothing; an active attacker could pose as either app, so these are **labelled “encrypted, not verified”**. Pair to be sure. |
| **Someone with a copy of your files** (a backup, a synced or copied folder, a stolen disk image) | Read the state folder | Pair keys and the browser sign-in key are sealed with a vault key kept in the OS keystore (see [Keys at rest](#keys-at-rest)); the copy alone opens nothing. Not where the keystore is a plain file (Docker, Linux without a keyring), which the app says. Files you receive are stored unencrypted. |
| **Malware running as you** | Anything you can | Out of scope on every desktop OS. On macOS it would at least trigger a Keychain prompt. |

Not in scope: hiding *that* two devices talk, file sizes, or timing; a
compromised OS on either end.

## Design

Built only from vetted parts in [`cryptography`](https://cryptography.io)
(pyca, OpenSSL underneath). No primitive is implemented here.

**`ot-file/2`: a handshake per transfer, then the same records as before.**

```
pair key K   32 bytes from SRP pairing (paired apps only; see trust-model.md)
kx_r         the receiver's per-run X25519 key (in its hellos; never stored)
salt         32 random bytes the sender picks per offer
T1           JSON["open-transfer/2", sender app, receiver app, job id, target, salt, eph_s, kx_r]
T2           T1 plus eph_r

offer   (sender → receiver; signed with X-OT-Auth when paired)
  eph_s   a fresh X25519 key, for this offer only
  PSK     = HKDF(K, salt, "open-transfer-psk/2")       paired; 32 zero bytes unpaired
  k1      = HKDF(X25519(eph_s, kx_r) ‖ PSK, salt, "open-transfer-offer/2" ‖ T1)
  meta    sealed with HKDF(k1, salt, "open-transfer-meta/2|…")   names, types, who's sending

answer  (receiver → sender, in the 201)
  eph_r   a fresh X25519 key, for this answer only
  confirm = HMAC-SHA256(HKDF(k1, "", "open-transfer-confirm/2"), T2)

session = HKDF(X25519(eph_s, eph_r) ‖ k1, salt, "open-transfer-session/2" ‖ T2)
file key    = HKDF(session, salt, "open-transfer-file/2|<sender>|<receiver>|<target>|<index>|<size>")
signal key  = HKDF(session, salt, "open-transfer-signal/2|<direction>|<sender>|<receiver>|<target>")
verify key  = HKDF(file key, "", "open-transfer-verify/2")
```

This is the shape of TLS 1.3's PSK + (EC)DHE key schedule, with a Finished-style
confirmation; every primitive is `cryptography`'s (X25519, HKDF-SHA256,
HMAC-SHA256, AES-256-GCM), checked against RFC 7748, RFC 5869, RFC 4231 and
the GCM paper's test vectors (`tests/test_handshake.py`). The composition
hasn't had an outside review or a formal proof.

* **Authentication.** Paired: the offer is signed with the pair key, and only
  the holder of the pair key *and* of `kx_r` can make `confirm`. Unpaired: PSK
  is zero, so it proves only that the answer came from whoever holds `kx_r`
  (“encrypted, not verified”).
* **Binding.** The version, both apps, the job, the recipient, the salt and
  every public key are in the transcript: a changed, replayed or moved answer
  fails `confirm`, and the sender refuses the transfer (never sends it plain).
* **Each recipient** of a group send gets its own offer, ephemeral key, salt and
  session; no key is shared between recipients.
* **Keys end with the transfer.** Each side drops its ephemeral private key as
  soon as the session key exists. Both drop the session key when the transfer
  is over (done, failed, canceled, expired) or the device is unpaired; a resume
  after that is refused and the next try is a new handshake. Python can't
  overwrite memory, so “drop” means no reference is left, not a wiped page.

**File contents: AES-256-GCM in STREAM records** (the online-AE construction
of Hoang, Reyhanitabar, Rogaway and Vizár, as used by `age` and Tink):

* The plaintext is cut into 64 KiB records; the last one may be shorter (an
  empty file is one empty record).
* Record *i* is `AES-256-GCM(file key, nonce = i as 11 bytes ‖ last-flag, plaintext)`
  and is 16 bytes longer than its plaintext.
* Each file has its own key, and the counter makes every nonce unique under
  it, so a nonce is never reused.
* The key binds sender, receiver, target, transfer, file index and size. A
  record moved to another position, file or transfer fails to decrypt. A file
  cut short fails too, because its last record must carry the last-flag and
  the size is part of the key.
* Ciphertext size = `size + 16 × max(1, ⌈size / 65536⌉)`.

**Metadata.** File names, types and who is sending are sealed with the meta
key (AES-256-GCM, random 96-bit nonce). The plain offer carries only
placeholders and sizes, which the receiver needs for its space check.

**Integrity confirmation.** After the last record, the sender confirms the
whole file with `HMAC(verify key, SHA-256 of the plaintext)` instead of the
bare hash. A bare hash over plain HTTP would let a listener confirm a guess
of the file's contents.

**Nothing secret leaves the app.** Keys are never in URLs, logs or API
responses; `/api/state` only says `"encrypted": true|false`.

### Negotiation and downgrade

1. Every app advertises `"enc": 2` and its per-run key `"kx"` in `/info` and in hellos.
2. When an app gets a **signed** hello carrying `enc: 2`, it records that in
   `trusted.json` (the `enc` flag). The flag is sticky until you unpair.
3. An offer to an app that advertised `kx` carries
   `"enc": {"v": 2, "salt", "eph", "kx", "meta"}`. Between paired apps the
   offer is signed, so nobody can remove that block in transit.
4. A receiver that supports `ot-file/2` answers `201 {…, "enc": 2, "eph", "confirm"}`
   and then accepts **only** encrypted bytes for that transfer.
5. If the answer lacks `enc: 2`, or its `confirm` doesn't check out:
   * and the receiver is flagged as supporting it, the sender **refuses**
     (“Couldn't set up encryption with *X*”). Something stripped the
     signature or the answer.
   * otherwise (an older app), the file goes as before and is labelled
     **Not encrypted — *X* needs an update** on both sides.

There is no silent fallback. A plain transfer is always visible as one, in
the transfer list and in history.

## Every path a file takes

| Path | Before | Now | Notes |
|---|---|---|---|
| Paired app → app, small file (single `PUT`) | Plain HTTP | **AES-256-GCM STREAM** | `_Sink._put_remote` encrypts and `receive_file` decrypts on the fly, 64 KiB at a time. |
| Paired app → app, big file (2 MiB `Content-Range` pieces, resumable) | Plain HTTP | **AES-256-GCM STREAM** | Each piece is 32 whole records. `Content-Range` stays in plaintext positions. The receiver decrypts each piece before staging it, so a resumed transfer still lines up. |
| Group send (one upload, many receivers) | Plain HTTP to each | **Per-receiver keys** | One plaintext stream fans out. Each receiver's thread encrypts with that pair's key. |
| Unpaired app ↔ app | Plain HTTP | **AES-256-GCM STREAM, one-off key**, labelled “encrypted, not verified” | X25519 between the sender's one-off key and the receiver's per-run key (never stored: forward secrecy), then HKDF as for a pair key. Contents and names are hidden from a listener. Not authenticated: someone who can answer in the receiver's place gets the file. A stripped key is refused, never sent plain. |
| Paired, but the other app is older | Plain HTTP | Plain HTTP, **labelled** | Until it updates. |
| App's own window → its app | Loopback | Loopback | Never on the network. |
| **Visitor browser → its app** (web page opened from a link or QR code) | Plain HTTP | Plain HTTP by default; **TLS with `--https`** | See [Browsers](#browsers). The visitor's page says “not encrypted” or “encrypted (HTTPS)”. |
| **App → visitor browser** (download, or pushed to a visitor) | Plain HTTP | Same as above | Same hop, same answer. |
| Android share sheet → the app | In-process | In-process | `content://` streams are copied inside the app (no network). |
| Over a phone's hotspot | — | Same as the path used | A hotspot is just another LAN: nothing changes per path. |
| Browser ↔ browser (WebRTC data channel) | DTLS | DTLS; **its SDP is MAC'd between apps** | Browsers always encrypt data channels; the file name and type travel inside DTLS. Between two apps the connection messages carry a MAC under the transfer's key ([Direct transfers](#direct-transfers-webrtc)). Each browser's own hop to its app is plain HTTP unless `--https`, so an active attacker *there* could still swap the DTLS fingerprints. |
| Clipboard sync | Sealed per recipient (`sync/1`) | Unchanged | See [sync.md](sync.md). |
| Android ↔ desktop, Windows ↔ macOS | — | Same as paired app ↔ app | Every app runs the same Python code (`cryptography` 42+ on Android via Chaquopy). |

## Browsers

A browser's own hop to its app can't use `ot-file/2`:

* a page on `http://<LAN address>` isn't a *secure context*, so it has no
  WebCrypto; JavaScript crypto from a library would be served over the same
  plain HTTP, so an active attacker could replace it;
* the browser shares no key with the app, and putting the device's long-term
  keys in JavaScript is out of the question.

The one sound answer is TLS, which gives the hop ephemeral ECDHE session keys
from the browser's own vetted stack. **`--https`** (`OPEN_TRANSFER_HTTPS=1`)
serves browsers on the next port over TLS 1.2+ with a self-signed P-256
certificate (`tls.py`); links and QR codes then use `https://`. Apps keep
talking to each other on the plain port, encrypted with `ot-file/2`.

* **Authentication.** There's no CA for a LAN address, so a browser warns
  once per device; the owner's Add device sheet shows the certificate's
  SHA-256 fingerprint to compare with the browser's certificate details. The
  certificate is kept (397 days, renewed 30 days before it ends), so the
  browser's "proceed" keeps holding.
* **The key** is sealed in the vault (`tls.json`), like the pair keys.
  Python's `ssl` loads keys only from files, so it's written to an
  owner-only temporary file for the moment it's read.
* **Off by default** because of that warning: Open Transfer stays "just open
  a link". Without it, a browser's hop is plain HTTP and its page says so
  (“· not encrypted”). On the device's own window (127.0.0.1) it's never on
  the network.
* **Old links.** With `--https`, a browser that opens a page on the plain
  port (an old `http://` link, a typed address) is redirected to the TLS
  port. The device's own window (127.0.0.1) and the apps' `/api/p2p` calls
  stay on the plain port. An active attacker can still strip that redirect
  for a browser that has never been to the `https://` address (no HSTS: a
  browser ignores it on a certificate it had to be told to accept).
* **WebRTC** (browser ↔ browser): see [Direct transfers](#direct-transfers-webrtc).

### What a browser's plain-HTTP hop shows

Without `--https`, everything between a visitor's browser and its app is
readable and changeable by someone on the same network, which means, in full:

* **The page itself** (HTML and JavaScript). An active attacker can serve a
  different page, which then does whatever it likes in that browser. No
  in-page check can fix this: the check would come over the same hop.
* **File contents, names, types and sizes** of everything the browser sends or
  receives through its app (relay), and its inbox listing.
* **Who's who:** the browser's chosen name and icon, the device list it sees,
  its visitor cookie (enough to act as that browser on that app until it
  leaves), and the PIN if it types one.
* **Transfer ids and secrets** the page uses, and **WebRTC connection
  messages**: the SDP (DTLS fingerprint, ICE candidates, i.e. local
  addresses). With these an active attacker can sit in the middle of a direct
  transfer. A passive one learns addresses only: the data channel is DTLS.

With `--https` all of this is inside TLS, once the person accepted the
certificate. Comparing its fingerprint with the one on the owner's Add device
sheet is what makes that acceptance an authentication rather than a guess.

### Options evaluated for the browser's hop

| Option | Verdict |
|---|---|
| TLS (`--https`) | **Done, opt-in.** The only thing a browser trusts that isn't sent over the hop it protects. Self-signed: one warning per device, then kept 397 days. |
| Certificate fingerprint check | **Manual**: the owner's sheet shows the SHA-256. A page can't check its own certificate (it would run on the connection it's checking), and a QR code carrying the fingerprint is only useful to a scanner app that pins it, which browsers' cameras don't. |
| Authenticating the browser's signaling | **Not possible over plain HTTP.** Any key the page used would be delivered by the same hop. Over TLS it's already authenticated as far as the certificate is. |
| Ephemeral session credentials | **Already there**: a visitor's cookie is random per browser, and a QR invite works for 10 minutes. They stop other browsers, not someone reading the hop. |
| Browser compatibility | TLS 1.2+, P-256: every browser that runs the page. WebRTC data channels are DTLS in every browser; the page falls back to the app if `RTCPeerConnection` is missing. |

So a **browser transfer is not end-to-end encrypted by Open Transfer.** Its
data channel is DTLS, but the DTLS keys are authenticated only as well as the
SDP that carried their fingerprints, which crossed each browser's hop to its
app: plain HTTP by default, TLS with `--https`.

## Direct transfers (WebRTC)

A page sending to a **browser** opens a WebRTC data channel to it. The file
then goes browser to browser, inside DTLS. Each browser checks the other's
DTLS certificate against the `a=fingerprint` in the SDP it was handed, so
**whoever can change an SDP on its way can sit in the middle** and read the
file. The SDP crosses up to three hops:

```
sender page ──(1)── its app ──(2)── receiver's app ──(3)── receiver page
```

| Hop | Protection |
|---|---|
| (1), (3): a page to its own app | The app's own window: loopback, never on the network. A visitor's browser: plain HTTP, or TLS with `--https` (above). |
| (2): app to app (only when the two browsers are on different apps) | **For an encrypted transfer, a MAC under that transfer's key.** A plain one (an older app): the session secret only, as before. |

On hop (2) each message carries a counter `n` and
`mac = HMAC-SHA256(K, JSON[dir, receiver's session id, sender's job id, target, kind, sdp, n])`,
where `K = HKDF(transfer key, salt, "open-transfer-signal/1|sender|receiver|target")`:

* the **transfer key** is the pair key (paired: only those two devices have
  it, so the SDP and its DTLS fingerprint are bound to the paired device's
  identity) or the transfer's one-off X25519 key (unpaired: bound to whoever
  answered the offer, “not verified” as for its files);
* the **salt** is that transfer's own, so a message can't be moved to another
  transfer, and the **target** is in the key and the MAC, so one receiver in a
  group can't answer for another;
* **`n`** must be above the last one taken for that transfer and direction, so
  a message can't be replayed (a replayed offer would restart the receiving
  browser's connection). A message lost on the way just makes the page try
  again with the next `n`;
* messages are refused unless the transfer is accepted and still running
  on that app.

The SDP isn't encrypted on hop (2): what it holds (a certificate fingerprint,
local addresses) isn't secret, and DTLS already encrypts the data. Only
changing it matters, and the MAC stops that. (Between paired apps the request
also carries an `X-OT-Auth` signature, like every request to a paired app; the
MAC is what's checked, since it also binds the transfer and the counter.)

| Attack on hop (2) | Result | Test (`tests/test_signaling.py`) |
|---|---|---|
| Changed DTLS fingerprint, ICE candidate, whole SDP, kind or counter | `403`, the page gets nothing; the real message still works after | `test_a_changed_message_is_refused` |
| MAC removed (a downgrade) or made with another key (another device) | `403` | same |
| Replayed message | `403` | `test_a_replayed_message_is_refused` |
| Message moved to another transfer, with that transfer's (sniffed) secret | `403` | `test_a_message_moved_to_another_transfer_is_refused` |
| Answer moved to another receiver in a group | `403` | `test_an_answer_moved_to_another_receiver_in_a_group_is_refused` |
| Message for a transfer the receiver canceled | `409` | `test_messages_for_a_finished_transfer_are_refused` |
| Message lost, sent again | Delivered once, with the next `n` | `test_a_lost_message_can_be_sent_again` |
| Older app (plain transfer) | Works without a MAC, labelled “not encrypted” | `test_an_older_app_still_swaps_messages_labelled_plain` |

Each runs for a paired and an unpaired (one-off key) transfer. If no
connection opens in ~12 s, or it breaks, the sender's page falls back to
sending through the apps (encrypted between them as usual).

## Key lifecycle

| Event | What happens | Tested |
|---|---|---|
| Pairing | SRP-6a leaves both apps with the 32-byte pair key ([trust-model](protocol.md#3-pairing)) | `test_mesh` pairing tests |
| Each transfer | A new 32-byte random salt, so every file key is new. The pair key itself never encrypts anything. | `test_every_transfer_gets_its_own_keys` |
| Each file | Its own key: index, size, target and both app ids are in the HKDF info | `test_ciphertext_from_another_file_or_transfer_is_refused` |
| Group send | Each receiver's file key comes from *its* pair key | `test_every_receiver_in_a_group_gets_its_own_key` |
| Unpair | The key is deleted. Encrypted transfers from that device stop, and new ones can't be keyed. | `test_unpairing_stops_transfers_from_that_device` |
| Re-pair / reinstall | A new pair key; the `enc` flag starts over and is learned again from the next signed hello | `test_re_pairing_gives_a_new_key_and_transfers_still_work` |
| History | Records hold names, sizes and states, never keys, so re-pairing never touches them | — |

### Rotation and revocation

| Key | Rotated | Revoked |
|---|---|---|
| Pair key | Only by pairing again (`test_re_pairing_gives_a_new_key_and_transfers_still_work`). Every transfer derives new keys from it with a fresh salt. | **Unpair**: this app deletes the key at once and stops what that device was sending with it (`test_unpairing_stops_transfers_from_that_device`). The other device isn't told unless both are in a sync network (then every member forgets it); its next send is refused as a downgrade, never sent plain, but it says “Couldn't set up encryption”, not “unpaired”. |
| One-off keys | The receiver's per run, the sender's per offer; never stored. | — |
| Vault key | `--rotate-keys` (app not running) | Delete the keystore item: pairings lock, and stay listed until paired again |
| TLS certificate | 30 days before it ends (397 days) | Delete `tls.json`: a new one next start (browsers warn again) |

### Forward secrecy

| What was recorded | Later stolen | Can it be read? |
|---|---|---|
| A finished transfer's file contents and WebRTC messages | The pair key | **No**: the session key also needs `X25519(eph_s, eph_r)`, and both ephemeral keys were dropped during the handshake (`test_a_stolen_pair_key_cant_decrypt_a_recorded_transfer`) |
| Its file names and sender | The pair key, while the receiver is still running the same session of the app | Yes: names are sealed under `k1`, which uses the receiver's per-run key (it can't use `eph_r`, which doesn't exist until the answer). Once that app restarts, no. |
| Anything | Memory of a running app mid-transfer | Yes: out of scope (malware running as you) |

Unpaired transfers get the same per-transfer forward secrecy, without the
authentication.

### Metadata a listener still sees

Between two apps, for an encrypted transfer, someone on the network still sees:

* **That two devices talk, when, and how much**: addresses, ports, timing,
  each file's size (in the offer and `Content-Length`/`Content-Range`) and how
  many files.
* **Each app's announcement** (multicast hellos and `/info`): its id, name,
  icon, platform, version, whether it takes files only from paired devices,
  its per-run key, and the names of browsers connected to it.
* **The sending app's announcement** inside each offer (`from`), and the
  target's id. File names, types and the person sending are sealed.
* **Transfer ids and secrets**, and WebRTC SDP (local addresses).

Hiding names in announcements would hide the device from people choosing whom
to send to, which is the point of them; sizes and timing are out of scope (see
the threat model).

## Keys at rest

Secrets in the state folder are **sealed** (AES-256-GCM) with a random
32-byte *vault key* that lives in the operating system's keystore
([`keystore.py`](../src/open_transfer/keystore.py)). Each sealed value names
its store and key id and is bound to what it is (`pair|<device id>`,
`session`), so it can't be moved onto another entry.

| Platform | Vault key kept in | Protects against |
|---|---|---|
| macOS | the login **Keychain** (generic password, default access: only this app reads it without a prompt) | Copies and backups of the state folder; other apps (they trigger a prompt) |
| Windows | `keys/<id>.dpapi`, encrypted with **DPAPI** for this user | Copies of the folder to another PC or user; offline disk reads without your Windows password |
| Android | `keys/<id>.wrapped`, wrapped by an AES key in the **Android Keystore** (hardware-backed where the phone has it; can't be exported) | Copies of the app's data (backups are off too: `allowBackup="false"`) |
| Linux desktop | the **Secret Service** keyring (GNOME Keyring, KWallet) via `secret-tool`; the key goes over stdin, never on a command line | Copies and backups of the folder |
| Docker, Linux without a keyring | `keys/<id>`, mode 0600: **a plain file, not OS-protected** | Nothing more than file permissions. `open-transfer --keys` and `/api/state` (`keys.protected: false`) say so; it's never called protected. `OPEN_TRANSFER_KEYSTORE=file` picks this store on purpose (the tests do). |

| What's sealed | File | Without its vault key |
|---|---|---|
| Pair keys | `trusted.json` (`"key": "ot-vault/1:…"`) | The pairing is **kept, locked**: never trusted, shown as “Couldn't unlock the pairing with *X* … Pair again”, and back as soon as the key is (keychain access restored) |
| Browser sign-in key | `secret` | A new one is made: browsers enter the PIN again |

**Migration.** An older `trusted.json` (keys in hex) and `secret` (raw
bytes) are sealed the first time the new version starts, in place; names,
`enc` flags and platforms are kept (`test_old_trusted_json_is_migrated…`).

**Recovery, never silent loss.**

* The keystore refuses or can't read back what it stored: the vault key goes
  to a file instead, labelled unprotected, and moves into the keystore by
  itself once that works again.
* The vault key is gone (reinstall, keychain reset, the folder copied to
  another computer, access denied): entries stay on disk locked and are
  never trusted (an empty key would let anyone sign as that device). Pairing
  again replaces a locked entry; unpairing removes it.
* A damaged entry is locked the same way, never dropped.
* `open-transfer --rotate-keys` (app stopped) reseals everything with a new
  vault key, then deletes the old one.

**macOS and updates.** An ad-hoc signed build counts as a new app after each
update, so macOS asks once to let it read its Keychain item (“Always
Allow”). Builds signed with a Developer ID keep their identity and won't
ask. If you click Deny, pairings are locked (above) until you allow it.

**Logs and API.** A `TrustedDevice` never prints its key (`repr=False`);
`/api/state` carries only the store's name, whether it's protected, and the
names of locked pairings.

## Review (nonces, keys, replay, metadata)

| Check | Finding |
|---|---|
| File nonces | Unique: every file has its own key (HKDF over the pair or one-off key, a random 32-byte salt per offer, both app ids, target, index, size), and the nonce is the record counter (11 bytes, 2⁸⁸ records) plus the last-flag. A piece sent again after an error is the *same* encrypted body (`_push_chunked` keeps it), never new bytes under an old nonce. **Retry** makes a new offer, so a new salt and new keys. |
| Metadata nonces | One seal per meta key (the key is new with every offer), random 96-bit nonce. |
| Vault nonces | Random 96-bit nonces under one vault key; every save reseals every key, but that's thousands of seals, far below the 2³² bound for random nonces. `--rotate-keys` starts a new key. |
| Key derivation | HKDF-SHA256 with a distinct label per use (`open-transfer-file/1`, `-meta/1`, `-verify/1`, `-eph/1`, `-signal/1`, `pair-key/2`). The pair key never encrypts anything itself. |
| One-off keys | X25519 from `cryptography`, which refuses a point that yields an all-zero secret (a 400, tested). The receiver's key lasts one run and is never stored; the sender's is new per offer. |
| Signaling | WebRTC messages between apps: HMAC-SHA256 under a per-transfer key, with a per-direction counter; see [Direct transfers](#direct-transfers-webrtc). |
| Replay | Signed requests carry a timestamp (±120 s) and are accepted once. A replayed record or piece fails: each transfer and file has its own key. Uploads need the session secret; between unpaired apps a listener can see that secret, but can't make records that decrypt. |
| Metadata integrity | Sizes are in the file key, so a changed size fails decryption. Names, types and sender are sealed (AES-GCM). The plain `from` of an unpaired offer is not authenticated, like everything about an unpaired app. |
| Partial files | A file is written to `.part` and kept only after its last record and the MAC check pass; on any failure the `.part` is deleted (`test_a_tampered_*`, `test_unpaired_tampering_leaves_nothing`, `test_a_canceled_encrypted_transfer_leaves_nothing`). |

## Failure reasons the user sees

| Cause | Receiver | Sender |
|---|---|---|
| Wrong key, tampered or injected record, records out of order, file cut short | File removed: “It arrived damaged or wasn't from *X* — nothing was kept.” | “*X* couldn't verify the file.” |
| A downgrade was attempted | — | “Couldn't set up encryption with *X*.” |
| “Encrypted transfers only”, the other app can't encrypt | The offer is refused: “*Y* takes only encrypted transfers. Update Open Transfer on this device to send to it.” | “Not sent: *X* can't encrypt (an older version?) and this device sends only encrypted transfers. Update Open Transfer on *X*, or turn off “Encrypted transfers only”.” |
| A WebRTC message was changed, moved or replayed | Refused (`403`); nothing reaches the page | The page's direct attempt times out and the file goes through the apps |
| Unpaired during a transfer | Transfers from that device stop: “No longer paired with *X*.” | Its next piece fails the same way. |
| Interrupted or app restarted | `.part` files and staged pieces are deleted (staged pieces after their usual expiry) | Retried or resumed as before; every piece is authenticated again. |

## Performance

`python scripts/bench_encryption.py --mb 1024` on an Apple M5 Mac
(Python 3.14, `cryptography` 50) gave the figures below. Both apps run in
**one process on 127.0.0.1**, so one CPU does the sender's encryption *and*
the receiver's decryption, and the network is never the limit. On real
devices each side does half that work.

| Mode | Streams | Size | Time | Throughput | CPU | Peak memory |
|---|---|---|---|---|---|---|
| plain | 1 | 1024 MB | 1.73 s | 591 MB/s | 3.78 s | 74 MB |
| encrypted | 1 | 1024 MB | 3.14 s | 327 MB/s | 6.31 s | 69 MB |
| plain | 4 at once | 1024 MB | 3.63 s | 282 MB/s | 10.35 s | 96 MB |
| encrypted | 4 at once | 1024 MB | 5.3 s | 193 MB/s | 13.88 s | 77 MB |

AES-256-GCM on its own (encrypt and decrypt, one core): about 2 GB/s.

* **Throughput.** Encrypted transfers run at 190–330 MB/s, several times
  what Wi‑Fi carries (roughly 10–120 MB/s), so on a real network you
  shouldn't notice a difference.
* **CPU.** About 1.3–1.7× the plain transfer's, split between the two
  devices.
* **Memory.** Flat: the whole process stays near 70 MB for a 1 GB file. A
  file is never held in memory, only one 64 KiB record per stream, or one
  2 MiB piece when resuming.

**Every OS in CI** (`bench_encryption.py --mb 256` on GitHub's runners, run
41614032411 on `4c9a80a`: shared, slower CPUs than the M5; same one-process setup):

| Runner | AES-GCM alone | Plain, 1 stream | One-off key, 1 stream | Paired, 1 stream | Paired, 4 at once | Peak memory | 1 KiB latency (plain / paired) |
|---|---|---|---|---|---|---|---|
| Linux x64 | 462 MB/s | 314 MB/s | 171 MB/s | 172 MB/s | 105 MB/s | 56–77 MB | 4.2 / 4.6 ms |
| Windows x64 | 371 MB/s | 258 MB/s | 195 MB/s | 187 MB/s | 164 MB/s | 57–74 MB | 27 / 12 ms |
| macOS arm64 | 1912 MB/s | 595 MB/s | 290 MB/s | 269 MB/s | 253 MB/s | 58–74 MB | 4.9 / 7.4 ms |

Shared runners vary from run to run (Windows paired, 4 at once, was 105 MB/s
in run 41222156917); read these as “several times Wi‑Fi”, not as exact figures.

**Android emulator** (the CI smoke test, phone → runner over the emulator's
network, 32 MB): 3.8 MB/s plain, 3.2 MB/s encrypted. The emulator's
networking is the limit there, not the cipher; it shows the encrypted path
works on Android and costs little, not what a phone does.

* **Not measured:** a real Android phone, and Windows on real hardware.
  On ARMv8 phones AES has hardware instructions, but their cost stays
  unmeasured until a device run. Latency is a few ms either way: a 1 KiB file
  is one record.

## Older versions

| Sender | Receiver | Result |
|---|---|---|
| New | New, paired | Encrypted (required) |
| New | Old, paired | Plain, labelled; refused once the receiver has advertised `enc` |
| Old | New, paired | Plain, labelled on the receiver |
| New | New, unpaired | Encrypted with a one-off key, labelled “not verified” |
| New | Old, unpaired (no `kx`) | Plain, labelled |
| Old | New, unpaired | Plain, labelled |
| Any | New, with “Encrypted transfers only” | Encrypted, or refused with what to do |
| New, with “Encrypted transfers only” | Any | Encrypted, or not sent, with what to do |

The policy:

* **Encrypted whenever both apps can.** Paired: always. Unpaired: with a
  one-off key whenever the receiver advertises one.
* **Never a silent downgrade.** Once a paired app has said (in a signed
  hello) that it speaks `ot-file/2`, a plain answer from it is refused, not
  sent plain. A stripped one-off key is refused too. WebRTC messages for an
  encrypted transfer without a MAC are refused.
* **A plain transfer is always labelled**: “Not encrypted” in the transfer
  list, the incoming sheet and history, on both sides.
* **“Encrypted transfers only”** (Add device sheet, `--encrypted-only` or
  `OPEN_TRANSFER_ENCRYPTED_ONLY=1`; off by default so older versions keep
  working) refuses every app-to-app transfer that can't be encrypted, both
  ways, with the reasons above. A browser's own hop is governed by `--https`,
  not by this switch.

Older versions are **simulated** in the tests (an app with `enc`, `kx` or the
offer's `enc` block removed: `tests/test_encrypted_transfers.py`,
`tests/test_encrypted_only.py`, `tests/test_signaling.py`); no released older
build was run against this one.

## Coverage

What each path gets, and only what's tested. “Real device” means checked on
physical hardware; none of these have been yet (device-testing.md).

| Transfer path | Encryption | Authentication | Integrity | Evidence |
|---|---|---|---|---|
| Paired app ↔ app | AES-256-GCM (`ot-file/2`); names and sender sealed | Yes: the pair key (SRP); offers signed | Yes: GCM per record, MAC'd checksum, sizes in the key | `test_encrypted_transfers.py`; CI on Linux, Windows, macOS, Android emulator. Real device: no |
| Unpaired app ↔ app | AES-256-GCM, one-off X25519 key | **No** (“not verified”): whoever answers the offer | Yes, against anyone without that key | `test_encrypted_transfers.py` (unpaired tests). Real device: no |
| WebRTC, app ↔ app hop | SDP not encrypted (not secret); data is DTLS | Paired: pair key. Unpaired: one-off key (not verified). Older app: session secret only | Yes for encrypted transfers: MAC, counter, state | `test_signaling.py`. Real browsers on two apps: no |
| Browser ↔ browser | DTLS (the browser's own) | Only as far as each browser's hop to its app: none on plain HTTP, the certificate with `--https`; loopback for an app's own window | DTLS; SDP as above | `test_mesh.py` signal tests (no real WebRTC in CI). Real device: no |
| Browser ↔ app | None by default; TLS 1.2+ with `--https` | `--https`: a self-signed certificate the person compares by fingerprint. Otherwise none (a PIN keeps strangers out, it doesn't protect the hop) | TLS with `--https`; none otherwise | `test_https.py`, `e2e/test_https_ui.py` (Chromium over TLS). Real device: no |
| Older app versions | None, labelled; refused with “Encrypted transfers only” | Paired: signed requests. Unpaired: none | None beyond TCP | Simulated (above) |
| Android Share ingestion | Not on a network: `content://` streams copied inside the app, then as the path it's sent on | — (an Android intent from the person's own phone) | In-process copy, then as the path it's sent on | Emulator smoke test in CI. Real device: no |
| Group transfers | Per receiver, as its path; each receiver its own keys | Per receiver | Per receiver; a WebRTC answer can't be moved to another receiver | `test_every_receiver_in_a_group_gets_its_own_key`, `test_unpaired_group_send_*`, `test_an_answer_moved_*` |
| Hotspot transfers | As the path used: a hotspot is a LAN | As the path used | As the path used | `test_hotspot.py` (simulated addresses). Real device: no |
