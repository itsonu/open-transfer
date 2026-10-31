# File transfer encryption (`ot-file/1`)

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

```
pair key K  (32 bytes, from SRP pairing; see trust-model.md)
salt        32 random bytes the sender picks per offer (the transfer id, in crypto terms)

file key    = HKDF-SHA256(ikm=K, salt=salt,
                info="open-transfer-file/1|<sender app>|<receiver app>|<target>|<file index>|<size>")
meta key    = HKDF-SHA256(ikm=K, salt=salt,
                info="open-transfer-meta/1|<sender app>|<receiver app>|<target>")
verify key  = HKDF-SHA256(ikm=file key, salt="", info="open-transfer-verify/1")
```

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

**Metadata.** Once the receiver has shown in a signed hello that it supports
`ot-file/1`, file names, types and who is sending are sealed with the meta key
(AES-256-GCM, random 96-bit nonce). The plain offer then carries only
placeholders and sizes, which the receiver needs for its space check.

**Integrity confirmation.** After the last record, the sender confirms the
whole file with `HMAC(verify key, SHA-256 of the plaintext)` instead of the
bare hash. A bare hash over plain HTTP would let a listener confirm a guess
of the file's contents.

**Nothing secret leaves the app.** Keys are never in URLs, logs or API
responses; `/api/state` only says `"encrypted": true|false`.

### Negotiation and downgrade

1. Every app advertises `"enc": 1` in `/info` and in hellos.
2. When an app gets a **signed** hello carrying `enc: 1`, it records that in
   `trusted.json` (the `enc` flag). The flag is sticky until you unpair.
3. An offer to a paired app always carries `"enc": {"v": 1, "salt": …}`. The
   offer is signed, so nobody can remove that block in transit.
4. A receiver that supports `ot-file/1` and verified the signature answers
   `201 {…, "enc": 1}` and then accepts **only** encrypted bytes for that
   transfer.
5. If the answer lacks `enc: 1`:
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
| Browser ↔ browser (WebRTC data channel) | DTLS | DTLS (unchanged) | Browsers always encrypt data channels. The file name and type travel inside DTLS. The connection messages (SDP) cross the visitor's plain-HTTP hop, so an active attacker there could swap the DTLS fingerprints and sit in the middle. A passive listener can't read anything. |
| Clipboard sync | Sealed per recipient (`sync/1`) | Unchanged | See [sync.md](sync.md). |
| Android ↔ desktop, Windows ↔ macOS | — | Same as paired app ↔ app | Every app runs the same Python code (`cryptography` 42+ on Android via Chaquopy). |

## Browsers

A browser's own hop to its app can't use `ot-file/1`:

* a page on `http://<LAN address>` isn't a *secure context*, so it has no
  WebCrypto; JavaScript crypto from a library would be served over the same
  plain HTTP, so an active attacker could replace it;
* the browser shares no key with the app, and putting the device's long-term
  keys in JavaScript is out of the question.

The one sound answer is TLS, which gives the hop ephemeral ECDHE session keys
from the browser's own vetted stack. **`--https`** (`OPEN_TRANSFER_HTTPS=1`)
serves browsers on the next port over TLS 1.2+ with a self-signed P-256
certificate (`tls.py`); links and QR codes then use `https://`. Apps keep
talking to each other on the plain port, encrypted with `ot-file/1`.

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
* **WebRTC** (browser ↔ browser) is always DTLS. Its connection messages
  (SDP, with the DTLS fingerprints) go over each browser's hop to its app:
  with `--https` that hop is protected. Two browsers on *different* apps also
  pass them over the apps' own link, which carries them as plain HTTP, so an
  active attacker there could still swap the fingerprints. Not closed.

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
| Key derivation | HKDF-SHA256 with a distinct label per use (`open-transfer-file/1`, `-meta/1`, `-verify/1`, `-eph/1`, `pair-key/2`). The pair key never encrypts anything itself. |
| One-off keys | X25519 from `cryptography`, which refuses a point that yields an all-zero secret (a 400, tested). The receiver's key lasts one run and is never stored; the sender's is new per offer. |
| Replay | Signed requests carry a timestamp (±120 s) and are accepted once. A replayed record or piece fails: each transfer and file has its own key. Uploads need the session secret; between unpaired apps a listener can see that secret, but can't make records that decrypt. |
| Metadata integrity | Sizes are in the file key, so a changed size fails decryption. Names, types and sender are sealed (AES-GCM). The plain `from` of an unpaired offer is not authenticated, like everything about an unpaired app. |
| Partial files | A file is written to `.part` and kept only after its last record and the MAC check pass; on any failure the `.part` is deleted (`test_a_tampered_*`, `test_unpaired_tampering_leaves_nothing`, `test_a_canceled_encrypted_transfer_leaves_nothing`). |

## Failure reasons the user sees

| Cause | Receiver | Sender |
|---|---|---|
| Wrong key, tampered or injected record, records out of order, file cut short | File removed: “It arrived damaged or wasn't from *X* — nothing was kept.” | “*X* couldn't verify the file.” |
| A downgrade was attempted | — | “Couldn't set up encryption with *X*.” |
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
41222156917: shared, slower CPUs than the M5; same one-process setup):

| Runner | AES-GCM alone | Plain, 1 stream | Encrypted, 1 stream | Encrypted, 4 at once | Peak memory | 1 KiB latency (plain / encrypted) |
|---|---|---|---|---|---|---|
| Linux x64 | 460 MB/s | 347 MB/s | 178 MB/s | 105 MB/s | 55–78 MB | 4.4 / 4.7 ms |
| Windows x64 | 460 MB/s | 227 MB/s | 143 MB/s | 105 MB/s | (fixed after this run) | 32 / 9 ms |
| macOS arm64 | 1839 MB/s | 465 MB/s | 253 MB/s | 173 MB/s | 61–73 MB | 4.3 / 9.8 ms |

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
