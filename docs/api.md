# HTTP API

Everything the web app does is available over a small JSON API, so you can script Open Transfer with `curl`, shortcuts or other tools.

- Base URL: the address printed on start, e.g. `http://192.168.1.24:5000`.
- Errors are JSON: `{"error": {"code": "too_large", "message": "File is larger than the 500 MB limit."}}` with a matching HTTP status.
- Browser requests that change data must be same-origin (CSRF protection). Non-browser clients like `curl` are unaffected.

## Authentication (PIN mode)

When started with `--pin`, every endpoint except `/api/health`, `/api/info` and `/api/auth` requires a session cookie.

```bash
curl -c jar -H 'Content-Type: application/json' -d '{"pin":"4821"}' http://HOST:5000/api/auth
curl -b jar http://HOST:5000/api/files
```

| Status | Meaning |
| ------ | ------- |
| `401 pin_required` | No valid session |
| `401 wrong_pin` | Wrong PIN |
| `429 too_many_attempts` | 5 wrong PINs in a minute; see `Retry-After` |

## Endpoints

### `GET /api/health`
`{"status": "ok", "version": "3.0.0"}` — for monitoring. Never requires a PIN.

### `GET /api/info`
Server name, version, auth state and — once authenticated — share URLs, permissions and limits.

```json
{
  "app": "Open Transfer", "version": "3.0.0", "device": "studio-mac",
  "auth": {"required": false, "authenticated": true, "numeric": false},
  "share_url": "http://192.168.1.24:5000",
  "urls": ["http://192.168.1.24:5000"],
  "permissions": {"upload": true, "browse": true, "delete": true},
  "limits": {"max_upload_size": 0, "chunk_size": 2097152, "chunk_threshold": 4194304},
  "storage": {"free": 182736451584},
  "pin": null
}
```

### `GET /api/files`
Newest first. Supports `If-None-Match` → `304 Not Modified`.

```json
{
  "files": [
    {"name": "Q3 Report.pdf", "size": 2412000, "modified": 1790276747.18,
     "mime": "application/pdf", "kind": "document"}
  ],
  "total_size": 2412000,
  "storage": {"free": 182736451584}
}
```

`kind` is one of `image video audio archive document spreadsheet presentation code app other`.

### `POST /api/files` — upload
**Raw body (recommended, streams any size):**

```bash
curl -T "Holiday video.mp4" -X POST \
  -H "X-Filename: $(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' 'Holiday video.mp4')" \
  http://HOST:5000/api/files
```

The file name goes in `X-Filename` (percent-encoded UTF-8) or a `?name=` query parameter.

**Multipart (one or more files):**

```bash
curl -F file=@photo.jpg -F file=@notes.txt http://HOST:5000/api/files
```

`/transfer` is kept as an alias for older scripts. Returns `201` with the saved files — names may get a ` (1)` suffix if taken:

```json
{"files": [{"name": "photo (1).jpg", "size": 48213, "modified": 1790276747.2, "mime": "image/jpeg", "kind": "image"}]}
```

Errors: `400 invalid_name`, `400 incomplete_upload`, `403 upload_disabled`, `413 too_large`, `507 insufficient_storage`.

**Chunked upload (files over ~4 MB):** the browser splits the file into 2 MiB
pieces (up to 3 in flight) so a dropped connection loses pieces, not the file.
Each piece is a `POST` with `Content-Range: bytes <start>-<end>/<total>` and an
`X-Upload-Id` the client picks (same id across retries, so a reload resumes):

```bash
curl -X POST --data-binary @chunk1 \
  -H "Content-Range: bytes 0-2097151/5242880" -H "X-Upload-Id: abc123" \
  -H "X-Filename: big.bin" http://HOST:5000/api/files
```

While incomplete the server answers `308` with `{"received", "total", "ranges"}`;
`201` carries the saved file once the last byte arrives. Pieces may arrive in
any order and rewrites are idempotent. `GET /api/uploads/<id>/status` reports
what arrived, so an interrupted upload resumes with the missing pieces only.

### `GET /files/<name>` — download
Always an attachment, with `Range` support for resuming (`curl -C - -O`). `?inline=1` displays images, audio and video in the browser. `/download/<name>` is a legacy alias.

### `DELETE /api/files/<name>`
Moves the file to the trash: `{"undo_token": "…", "undo_seconds": 30}`. `403 delete_disabled` if turned off.

### `POST /api/trash/<token>/restore`
Restores a deleted file within `undo_seconds`: `{"file": {…}}`, or `404`.

### `GET /api/archive`
Streams a ZIP of all files, or only those given as `?name=a.txt&name=b.jpg`.

### `GET /api/qr.svg`
QR code (SVG) for the share URL. With a PIN set it carries a 10-minute join token (`?join=`), never the PIN.

### `POST /api/logout`
Clears the session.

## Nearby devices (web UI endpoints)

These power the device grid. "Owner" means a request from the device itself
(`127.0.0.1`); everyone else is a visitor. App-to-app endpoints
(`/api/p2p/v1/*`) are documented in [protocol.md](protocol.md).

| Endpoint | Who | What |
| -------- | --- | ---- |
| `GET /api/state` | anyone | `{me, host, devices, incoming, outgoing, discovery, pairing?, inbox?}` with an `ETag` (poll with `If-None-Match`) |
| `POST /api/me` `{name, form?, platform?}` | anyone | Rename this device (owner) or how this browser appears (visitor) |
| `POST /api/send` `{to: [ids], files: [{name, size, mime}]}` | anyone | Offer files to devices → `201 {job}` |
| `PUT /api/send/<job>/files/<n>` | job creator | The file's bytes (`Content-Length` required), streamed to every receiver that accepted. Big files arrive as `Content-Range` chunks (see above) staged under `relay-chunks/`, then fanned out when complete |
| `GET /api/send/<job>/files/<n>/status?upload_id=…` | job creator | Staged bytes/ranges for one chunked file (resume + pause support) |
| `POST /api/invites` `{}` → `{token, url, expires_in}` · `GET /api/invites` | owner | Mint a single-use guest link (`/?invite=…`): lets one visitor past the PIN as a trusted guest, then expires (10 min default) |
| `DELETE /api/send/<job>` · `DELETE /api/send/<job>/targets/<id>` | job creator | Cancel everything / one receiver |
| `POST /api/send/<job>/retry` `{to?: [ids]}` | job creator | Re-offer the job's files to declined/failed/expired targets → `201 {job}` (receiver is asked again) |
| `POST /api/incoming/<id>/accept` · `/decline` · `DELETE /api/incoming/<id>` | the recipient | Answer or stop an incoming transfer |
| `GET /api/inbox/files/<name>` · `DELETE …` · `GET /api/inbox/archive` | visitor | Files sent to this browser |
| `POST /api/pair` `{code, address?}` | owner | Pair with the app showing `code` (the nearby one with Add device open, or at `address`) |
| `POST /api/pair/open` · `POST /api/pair/close` | owner | Add device opened (renew every ≤30 s while it shows) → `{code, expires_in}` · closed. Codes only work while it's open |
| `POST /api/devices` `{address}` | owner | Add an app by `host:port` |
| `DELETE /api/pairs/<id>` · `POST /api/pair/new-code` · `GET /api/pair/qr.svg` | owner | Unpair · new code · QR with the code |

### Clipboard sync ([Trusted Sync Network](sync.md))

| Endpoint | Who | What |
| -------- | --- | ---- |
| `GET /api/sync` | owner | `{active, paused, policy, clipboard_access, pending, waiting_messages, recent, devices: [{id, name, platform, online, syncing, excluded, paused_until, last_sent?, last_received?, last_error?}]}`. Never clip contents |
| `PUT /api/sync/policy` `{enabled?, target?, mode?, types?, expiry?, sensitive?, blocked_sources?, accept_introductions?, session_minutes?}` | owner | Change settings (see the policy table in sync.md) → status |
| `POST /api/sync/pause` `{minutes: n \| null}` · `POST /api/sync/resume` | owner | Pause everything for n minutes or until resumed |
| `PUT /api/sync/devices/<id>` `{excluded?, pause_minutes?: n \| null \| 0}` | owner | Per-device: leave out of "all devices", pause (0 = resume) |
| `DELETE /api/sync/devices/<id>` | owner | Remove the device here **and on every member** (revocation) |
| `POST /api/sync/send` `{text, confirm?}` | owner | Send now (also in "only when I ask" mode). `409 sensitive` until `confirm: true` for private-looking text |
| `POST /api/sync/leave` | owner | Leave the network: every reachable member forgets this device → `{told, missed}` |
| `GET /api/sync/clients` · `POST /api/sync/clients` `{name}` · `DELETE /api/sync/clients/<id>` | owner | iPhones/iPads using this device as their hub. Adding one returns its `token` (once) and the `push_url`/`latest_url` for its Shortcuts |
| `POST /api/sync/client/push` `{text, confirm?}` (or the text as the body) + `Authorization: Bearer <token>` | an iPhone/iPad | Send its clipboard: applied here and sent to the network → `{status, delivered, queued, refused, message}`; `409 sensitive` until `confirm` |
| `GET /api/sync/client/latest` + `Authorization: Bearer <token>` | an iPhone/iPad | `{text, from, message}`: the newest clip from any of your devices, until it expires (`text` is empty when there's nothing new) |
| `GET /api/app` | owner | `{desktop: false}`, or in the desktop app `{desktop, background, can_start_at_login, start_at_login}` |
| `PUT /api/app` `{start_at_login}` | owner | Start the background copy at login (desktop app) |
| `POST /api/app/quit` | owner | Quit the background copy (sharing and sync stop) |

Sending from a script, end to end:

```bash
APP=http://127.0.0.1:5000                      # your own app (owner)
TO=$(curl -s $APP/api/state | jq -r '.devices[] | select(.name=="Gaming PC") | .id')
JOB=$(curl -s -X POST $APP/api/send -H 'Content-Type: application/json' \
  -d "{\"to\":[\"$TO\"],\"files\":[{\"name\":\"report.pdf\",\"size\":$(stat -c%s report.pdf)}]}" | jq -r .job.id)
# …wait until .outgoing[].targets[].state is "accepted", then:
curl -X PUT --data-binary @report.pdf $APP/api/send/$JOB/files/0
```
