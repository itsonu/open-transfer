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
  "limits": {"max_upload_size": 0},
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

### `GET /files/<name>` — download
Always an attachment, with `Range` support for resuming (`curl -C - -O`). `?inline=1` displays images, audio and video in the browser. `/download/<name>` is a legacy alias.

### `DELETE /api/files/<name>`
Moves the file to the trash: `{"undo_token": "…", "undo_seconds": 30}`. `403 delete_disabled` if turned off.

### `POST /api/trash/<token>/restore`
Restores a deleted file within `undo_seconds`: `{"file": {…}}`, or `404`.

### `GET /api/archive`
Streams a ZIP of all files, or only those given as `?name=a.txt&name=b.jpg`.

### `GET /api/qr.svg`
QR code (SVG) for the classic share URL; includes the PIN when one is set (the pairing QR, `/api/pair/qr.svg`, never does).

### `POST /api/logout`
Clears the session.

## Nearby devices (web UI endpoints)

These power the device grid. "Owner" means a request from the device itself
(`127.0.0.1`); everyone else is a visitor. App-to-app endpoints
(`/api/p2p/v1/*`) are documented in [protocol.md](protocol.md).

| Endpoint | Who | What |
| -------- | --- | ---- |
| `GET /api/state` | anyone | `{me, host, devices, incoming, outgoing, discovery, pairing?, pair_requests?, multicast?, inbox?}`; devices may carry `last_seen`, `short_id` (same-name devices) and `stale` (an old install) with an `ETag` (poll with `If-None-Match`) |
| `POST /api/me` `{name, form?, platform?}` | anyone | Rename this device (owner) or how this browser appears (visitor) |
| `POST /api/send` `{to: [ids], files: [{name, size, mime}]}` | anyone | Offer files to devices → `201 {job}` |
| `PUT /api/send/<job>/files/<n>` | job creator | The file's bytes (`Content-Length` required), streamed to every receiver that accepted |
| `DELETE /api/send/<job>` · `DELETE /api/send/<job>/targets/<id>` | job creator | Cancel everything / one receiver |
| `POST /api/incoming/<id>/accept` · `/decline` · `DELETE /api/incoming/<id>` | the recipient | Answer or stop an incoming transfer |
| `GET /api/inbox/files/<name>` · `DELETE …` · `GET /api/inbox/archive` | visitor | Files sent to this browser |
| `POST /api/pair/open` · `POST /api/pair/close` | owner | Open (renew every ≤30 s) or close the pairing window; returns `{code, expires_in, open, address}` |
| `POST /api/pair` `{code, address?, device_id?}` | owner | Pair with the device showing `code`: the chosen one, the one at `address`, or any reachable device with its window open. Waits for the other owner's *Allow*. Errors carry `code`, `message`, `action` (see docs/trust-model.md) |
| `POST /api/pair/requests/<id>/allow` · `…/deny` | owner | Answer "Pair with …?" (`pair_requests[]` in `/api/state`) |
| `DELETE /api/devices/<id>` | owner | Remove a device from the list (unpairs it on both sides when reachable) |
| `POST /api/devices` `{address}` | owner | Add an app by `host:port` |
| `DELETE /api/pairs/<id>` · `POST /api/pair/new-code` · `GET /api/pair/qr.svg` | owner | Unpair (both sides) · new code · QR with the code and device id (never the PIN) |
| `GET /api/history?direction=sent\|received&failed=1&device=<id>&q=<text>&limit=<n>&before=<created_at>,<row_id>` | owner | Transfer history, newest first (≤200 per page). Each record: `transfer_id`, `direction`, `sender`, `recipients[]` (per-recipient `state`, `reason_code`, `reason`, `bytes_done`, `files_done`), `files[]` (with `exists` for received files), `state`, timestamps, and `summary` (`total`, `delivered`, per-state counts, `disconnected`); each recipient also has `files_failed`, `started_at`, `finished_at`, `disconnected` |
| `DELETE /api/history/<row_id>` · `POST /api/history/clear` `{completed_only}` | owner | Forget one finished record · forget finished (or only completed) records. **Files are never deleted.** |

Sending from a script, end to end:

```bash
APP=http://127.0.0.1:5000                      # your own app (owner)
TO=$(curl -s $APP/api/state | jq -r '.devices[] | select(.name=="Gaming PC") | .id')
JOB=$(curl -s -X POST $APP/api/send -H 'Content-Type: application/json' \
  -d "{\"to\":[\"$TO\"],\"files\":[{\"name\":\"report.pdf\",\"size\":$(stat -c%s report.pdf)}]}" | jq -r .job.id)
# …wait until .outgoing[].targets[].state is "accepted", then:
curl -X PUT --data-binary @report.pdf $APP/api/send/$JOB/files/0
```
