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

## Failure reasons the user sees

| Cause | Receiver | Sender |
|---|---|---|
| Wrong key, tampered or injected record, records out of order, file cut short | File removed: “It arrived damaged or wasn't from *X* — nothing was kept.” | “*X* couldn't verify the file.” |
| A downgrade was attempted | — | “Couldn't set up encryption with *X*.” |
| Unpaired during a transfer | Transfers from that device stop: “No longer paired with *X*.” | Its next piece fails the same way. |
| Interrupted or app restarted | `.part` files and staged pieces are deleted (staged pieces after their usual expiry) | Retried or resumed as before; every piece is authenticated again. |

## Older versions

| Sender | Receiver | Result |
|---|---|---|
| New | New, paired | Encrypted (required) |
| New | Old, paired | Plain, labelled; refused once the receiver has advertised `enc` |
| Old | New, paired | Plain, labelled on the receiver |
| Any | Unpaired | Plain, labelled |
