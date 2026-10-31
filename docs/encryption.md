# File transfer encryption (`ot-file/1`)

Files sent between two **paired** apps are encrypted end to end: only the
receiving app can read them, and any change, reordering, replay or
truncation is detected before a file is kept. This page covers the threat
model, the design, every path a file can take, and what is still in plain
text.

## Threat model

| Who | Can | Defended |
|---|---|---|
| **Passive listener** on the Wi‑Fi or hotspot (another guest, a compromised router) | Read every packet | Paired app ↔ app: file contents and file names are unreadable. Sizes and timing are visible. |
| **Active attacker** on the LAN (ARP spoofing, rogue hotspot, a spoofed multicast packet) | Change, drop, replay or inject packets; strip headers | Changed or injected bytes are rejected per 64 KiB record. Replays into another file or transfer fail because each uses a different key. Truncation fails. A downgrade to plain text is refused (see below). Dropping packets can still stop a transfer, which is a denial of service, not a disclosure. |
| **A paired device** | Decrypt what was sent *to it* | Each recipient's key is its own pair key, so in a group send, B can't decrypt what went to C. |
| **An unpaired app** | Send offers (asked first), receive what you choose to send it | No shared key: transfers to and from unpaired apps are plain HTTP and are **labelled “Not encrypted”**. |
| **Someone with your disk** | Read `trusted.json` | Out of scope. Pair keys sit in `<state>/trusted.json` with mode 0600 (owner only), like an SSH key. Files you receive are stored unencrypted. |

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
| Unpaired app ↔ app | Plain HTTP | Plain HTTP, **labelled** | There is no key to use. Pair the devices to encrypt. |
| Paired, but the other app is older | Plain HTTP | Plain HTTP, **labelled** | Until it updates. |
| App's own window → its app | Loopback | Loopback | Never on the network. |
| **Visitor browser → its app** (web page opened from a link or QR code) | Plain HTTP | Plain HTTP | A page on `http://<lan-ip>` isn't a secure context (no WebCrypto) and shares no key. **Remaining plaintext path.** |
| **App → visitor browser** (download, or pushed to a visitor) | Plain HTTP | Plain HTTP | Same reason. **Remaining plaintext path.** |
| Browser ↔ browser (WebRTC data channel) | DTLS | DTLS (unchanged) | Browsers always encrypt data channels. The file name and type travel inside DTLS. The connection messages (SDP) cross the visitor's plain-HTTP hop, so an active attacker there could swap the DTLS fingerprints and sit in the middle. A passive listener can't read anything. |
| Clipboard sync | Sealed per recipient (`sync/1`) | Unchanged | See [sync.md](sync.md). |
| Android ↔ desktop, Windows ↔ macOS | — | Same as paired app ↔ app | Every app runs the same Python code (`cryptography` 42+ on Android via Chaquopy). |

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

**At rest.** Pair keys live in `<state>/trusted.json`, written atomically
with mode 0600. By default `<state>` is `<storage folder>/.open-transfer`,
which is never served: hidden names are refused (`test_storage`). On
Windows, mode bits don't apply; the file has the folder's ACL (the user's
profile). On Android it's in app-private storage. Moving the keys into the
OS keystore (Keychain, DPAPI, Android Keystore) is a backlog item. Anyone
who can read your user files can already read the files you received.

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
* **Not measured here.** Android phones (no CI runner has one) and
  Windows. On ARMv8 phones AES has hardware instructions, but the cost
  there is unmeasured until a device run.

## Older versions

| Sender | Receiver | Result |
|---|---|---|
| New | New, paired | Encrypted (required) |
| New | Old, paired | Plain, labelled; refused once the receiver has advertised `enc` |
| Old | New, paired | Plain, labelled on the receiver |
| Any | Unpaired | Plain, labelled |
