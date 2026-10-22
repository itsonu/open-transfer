"""HTTP routes for nearby devices.

``/api/p2p/v1/*`` is spoken **between apps** (no browser, no PIN — receivers
always confirm, or the request is signed by a paired device).
Everything else here is for the web UI of the owner or a visitor.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import segno
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    request,
    session,
    stream_with_context,
)
from werkzeug.exceptions import ClientDisconnected

from open_transfer.archive import zip_stream
from open_transfer.clipboard import MAX_IMAGE_BYTES as CLIP_MAX_IMAGE
from open_transfer.devices import clean_form, clean_name, clean_platform, valid_id
from open_transfer.mesh import P2P, Mesh, MeshError, Viewer
from open_transfer.security import log_safe
from open_transfer.sensitive import REASONS, looks_sensitive
from open_transfer.serve import serve_file
from open_transfer.storage import IncompleteUpload, StorageError

PUBLIC_P2P_ENDPOINTS = {
    "p2p_info",
    "p2p_hello",
    "p2p_offer",
    "p2p_offer_status",
    "p2p_offer_upload",
    "p2p_offer_file_status",
    "p2p_offer_cancel",
    "p2p_pair",
    "p2p_pair_confirm",
    "p2p_signal",
}
MAX_PENDING_PER_CLIENT = 10
DRAIN_LIMIT = 8 * 1024 * 1024


def _drain_body(limit: int = DRAIN_LIMIT) -> None:
    """Read what's left of a small request body before answering with an error.

    Replying without reading the body makes some systems (Windows) reset the
    connection, so the client sees "connection reset" instead of our message.
    Big bodies are left alone; the connection is simply closed.
    """
    length = request.content_length
    if length is None or length > limit:
        return
    try:
        while request.stream.read(256 * 1024):
            pass
    except (ClientDisconnected, ConnectionError, OSError):
        return


def register(
    app: Flask,
    mesh: Mesh,
    *,
    viewer: Callable[[], Viewer],
    client: Callable[[], str],
    file_csp: str,
    share_url: Callable[[], str],
) -> None:
    config = mesh.config

    def body_json() -> dict[str, Any]:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise MeshError(400, "bad_request", "Expected a JSON object.")
        return data

    def signed_by(sender: str | None) -> bool:
        return mesh.verify_request(
            sender,
            request.headers.get("X-OT-Auth"),
            request.method,
            request.path,
            request.get_data(cache=True),
        )

    def too_big_for_clip() -> None:
        # Base64 of a 10 MB image plus the JSON around it.
        if (request.content_length or 0) > CLIP_MAX_IMAGE * 4 // 3 + 64 * 1024:
            raise MeshError(413, "too_large", "That is too big to share (10 MB max).")

    def owner_only() -> Viewer:
        current = viewer()
        if not current.is_owner:
            abort(403, "Only the person using this device can do that.")
        return current

    @app.errorhandler(MeshError)
    def mesh_error(exc: MeshError) -> Response:
        response = jsonify({"error": {"code": exc.code, "message": str(exc)}})
        response.status_code = exc.status
        return response

    # =============================================================== app ↔ app

    @app.get(f"{P2P}/info")
    def p2p_info() -> Response:
        sender = request.headers.get("X-OT-From")
        return jsonify(mesh.self_info(include_visitors=not config.pin or signed_by(sender)))

    @app.post(f"{P2P}/hello")
    def p2p_hello() -> Response:
        info = body_json()
        reply = mesh.handle_hello(info, client())
        if config.pin and not signed_by(str(info.get("id", ""))):
            reply.pop("visitors", None)
        return jsonify(reply)

    @app.post(f"{P2P}/offers")
    def p2p_offer() -> tuple[Response, int]:
        data = body_json()
        sender = data.get("from")
        origin = data.get("origin")
        if not isinstance(sender, dict) or not isinstance(origin, dict):
            raise MeshError(400, "bad_request", "Missing sender.")
        sender_id = str(sender.get("id", ""))
        verified = request.headers.get("X-OT-From") == sender_id and signed_by(sender_id)
        mesh.handle_hello(sender, client())
        if mesh.pending_from(client()) >= MAX_PENDING_PER_CLIENT:
            raise MeshError(429, "busy", "Too many unanswered transfers. Try again shortly.")
        target = str(data.get("to", ""))
        session = mesh.create_offer(
            origin=_clean_origin(origin, sender),
            target=target,
            files=data.get("files"),  # type: ignore[arg-type]
            paired=verified and origin.get("id") == sender_id,
            source=client(),
            job=str(data.get("job") or ""),
            sender_node=sender_id,
        )
        return jsonify(
            {
                "id": session.id,
                "secret": session.secret,
                "state": session.state,
                "reason": session.reason,
            }
        ), 201

    @app.get(f"{P2P}/offers/<session_id>")
    def p2p_offer_status(session_id: str) -> Response:
        return jsonify(mesh.offer_status(session_id, request.headers.get("X-OT-Secret", "")))

    @app.put(f"{P2P}/offers/<session_id>/files/<int:index>")
    def p2p_offer_upload(session_id: str, index: int) -> tuple[Response, int]:
        from open_transfer.chunks import parse_content_range

        # Chunked resume: apps sending big files can split them into
        # ``Content-Range`` pieces; a lost connection resumes mid-file.
        content_range = request.headers.get("Content-Range")
        if content_range:
            parsed = parse_content_range(content_range)
            if parsed is None:
                raise MeshError(400, "bad_range", "Invalid Content-Range header.")
            start, end, total = parsed
            try:
                data = request.get_data(cache=False)
            except (ClientDisconnected, ConnectionError) as exc:
                raise IncompleteUpload("The transfer was interrupted before it finished.") from exc
            if len(data) != end - start + 1:
                raise MeshError(400, "bad_range", "Chunk size doesn’t match Content-Range.")
            try:
                result = mesh.receive_chunk(
                    session_id,
                    index,
                    start,
                    data,
                    total,
                    secret=request.headers.get("X-OT-Secret", ""),
                )
            except (MeshError, StorageError):
                _drain_body()
                raise
            return jsonify(result), 201 if result.get("done") else 308
        try:
            saved = mesh.receive_file(
                session_id,
                index,
                request.stream,
                request.content_length,
                secret=request.headers.get("X-OT-Secret", ""),
            )
        except (ClientDisconnected, ConnectionError) as exc:
            raise IncompleteUpload("The transfer was interrupted before it finished.") from exc
        except (MeshError, StorageError):
            _drain_body()
            raise
        return jsonify({"file": saved.to_dict()}), 201

    @app.get(f"{P2P}/offers/<session_id>/files/<int:index>/status")
    def p2p_offer_file_status(session_id: str, index: int) -> Response:
        """Bytes staged for one incoming file (lets senders resume mid-file)."""
        try:
            return jsonify(
                mesh.incoming_chunk_status(
                    session_id, index, request.headers.get("X-OT-Secret", "")
                )
            )
        except (MeshError, StorageError):
            _drain_body()
            raise

    @app.delete(f"{P2P}/offers/<session_id>")
    def p2p_offer_cancel(session_id: str) -> Response:
        mesh.cancel_incoming(session_id, None, secret=request.headers.get("X-OT-Secret", ""))
        return jsonify({"ok": True})

    @app.post(f"{P2P}/clip")
    def p2p_clip() -> Response:
        too_big_for_clip()
        data = body_json()
        sender = data.get("from")
        sender_id = str(sender.get("id", "")) if isinstance(sender, dict) else ""
        verified = request.headers.get("X-OT-From") == sender_id and signed_by(sender_id)
        return jsonify(mesh.handle_clip(sender_id, verified, data))

    @app.post(f"{P2P}/signal")
    def p2p_signal() -> Response:
        mesh.handle_signal(body_json())
        return jsonify({"ok": True})

    @app.post(f"{P2P}/pair")
    def p2p_pair() -> Response:
        data = body_json()
        device = data.get("device")
        if not isinstance(device, dict):
            raise MeshError(400, "bad_request", "Missing device.")
        return jsonify(mesh.pair_begin(device, str(data.get("nonce", "")), client()))

    @app.post(f"{P2P}/pair/confirm")
    def p2p_pair_confirm() -> Response:
        data = body_json()
        return jsonify(
            mesh.pair_confirm(str(data.get("id", "")), str(data.get("proof", "")), client())
        )

    # ================================================================ web UI

    # ------------------------------------------------------------ clipboard

    @app.get("/api/clipboard")
    def ui_clipboard() -> Response:
        owner_only()
        return jsonify(
            {
                "items": mesh.clipboard.items(str(request.args.get("q", ""))[:200]),
                "settings": mesh.clipboard.settings(),
                "devices": mesh.clipboard_devices(),
            }
        )

    @app.put("/api/clipboard/settings")
    def ui_clipboard_settings() -> Response:
        owner_only()
        data = body_json()
        settings = mesh.set_clipboard_settings(
            allowed=data.get("allowed"),
            max_items=data.get("max_items"),
            max_age_days=data.get("max_age_days"),
        )
        return jsonify({"settings": settings, "devices": mesh.clipboard_devices()})

    @app.post("/api/clipboard")
    def ui_clipboard_share() -> Response:
        owner_only()
        too_big_for_clip()
        data = body_json()
        from open_transfer.mesh import _clean_clip

        kind, text, raw, mime = _clean_clip(data)
        reason = looks_sensitive(text) if kind == "text" else None
        if reason and data.get("confirm_sensitive") is not True:
            response = jsonify(
                {
                    "error": {
                        "code": "sensitive",
                        "reason": reason,
                        "message": f"This looks like {REASONS[reason]}. Share it anyway?",
                    }
                }
            )
            response.status_code = 409
            return response
        return jsonify(mesh.share_clipboard(kind=kind, text=text, data=raw, mime=mime))

    @app.delete("/api/clipboard")
    def ui_clipboard_clear() -> Response:
        owner_only()
        mesh.clipboard.clear()
        return jsonify({"ok": True})

    @app.delete("/api/clipboard/<item_id>")
    def ui_clipboard_delete(item_id: str) -> Response:
        owner_only()
        if not mesh.clipboard.delete(item_id):
            raise MeshError(404, "not_found", "That item isn’t in the clipboard history.")
        return jsonify({"ok": True})

    @app.get("/api/clipboard/<item_id>/image")
    def ui_clipboard_image(item_id: str) -> Response:
        owner_only()
        path = mesh.clipboard.image_path(item_id)
        if path is None:
            raise MeshError(404, "not_found", "That image isn’t in the clipboard history.")
        return serve_file(path, inline=True)

    @app.get("/api/state")
    def ui_state() -> Response:
        payload = json.dumps(mesh.state_for(viewer()), separators=(",", ":")).encode()
        etag = hashlib.blake2b(payload, digest_size=12).hexdigest()
        if request.if_none_match.contains(etag):
            return Response(status=304, headers={"ETag": f'"{etag}"'})
        response = Response(payload, mimetype="application/json")
        response.set_etag(etag)
        return response

    @app.post("/api/me")
    def ui_me() -> Response:
        data = body_json()
        current = viewer()
        if current.is_owner:
            if data.get("name"):
                mesh.identity_store.rename(str(data["name"]))
                mesh._bump()
        else:
            session["wname"] = clean_name(data.get("name") or current.name, "Browser")
            session["wform"] = clean_form(data.get("form") or current.form)
            session["wplat"] = clean_platform(data.get("platform") or current.platform)
        return jsonify(mesh.state_for(viewer())["me"])

    @app.post("/api/send")
    def ui_send() -> tuple[Response, int]:
        current = viewer()
        if not current.is_owner and not config.allow_upload:
            raise MeshError(403, "send_disabled", "Sending is turned off on this device.")
        data = body_json()
        targets = data.get("to")
        if not isinstance(targets, list):
            raise MeshError(400, "no_targets", "Choose at least one device.")
        job = mesh.create_job(current, targets, data.get("files"))  # type: ignore[arg-type]
        return jsonify({"job": mesh._job_view(job)}), 201

    @app.put("/api/send/<job_id>/files/<int:index>")
    def ui_send_file(job_id: str, index: int) -> Response:
        from open_transfer.chunks import parse_content_range

        current = viewer()
        # Chunked path: big files arrive as 2 MiB pieces with
        # ``Content-Range: bytes <start>-<end>/<total>`` + ``X-Upload-Id``.
        # One dropped chunk costs one retry, not the whole file.
        content_range = request.headers.get("Content-Range")
        if content_range:
            parsed = parse_content_range(content_range)
            if parsed is None:
                raise MeshError(400, "bad_range", "Invalid Content-Range header.")
            start, end, _total = parsed
            upload_id = (request.headers.get("X-Upload-Id") or "").strip()[:64]
            if not upload_id:
                raise MeshError(400, "bad_request", "Missing X-Upload-Id header.")
            try:
                data = request.get_data(cache=False)
            except (ClientDisconnected, ConnectionError) as exc:
                raise IncompleteUpload("The upload was interrupted before it finished.") from exc
            if len(data) != end - start + 1:
                raise MeshError(400, "bad_range", "Chunk size doesn’t match Content-Range.")
            try:
                result = mesh.relay_append(job_id, current, index, upload_id, start, data)
            except (MeshError, StorageError):
                _drain_body()
                raise
            status = 201 if result.get("done") else 308
            response = jsonify(result)
            response.status_code = status
            if status == 308:
                response.headers["Range"] = (
                    f"bytes=0-{result['received'] - 1}" if result["received"] else "bytes=*/0"
                )
            return response
        try:
            result = mesh.job_upload(job_id, current, index, request.stream, request.content_length)
        except (ClientDisconnected, ConnectionError) as exc:
            raise IncompleteUpload("The upload was interrupted before it finished.") from exc
        except (MeshError, StorageError):
            _drain_body()
            raise
        return jsonify(result)

    @app.get("/api/send/<job_id>/files/<int:index>/status")
    def ui_send_file_status(job_id: str, index: int) -> Response:
        """How many bytes of a chunked upload already arrived (for resume)."""
        current = viewer()
        upload_id = (request.args.get("upload_id") or "").strip()[:64]
        if not upload_id:
            raise MeshError(400, "bad_request", "Missing upload_id.")
        return jsonify(mesh.relay_status(job_id, current, index, upload_id))

    @app.delete("/api/send/<job_id>")
    def ui_send_cancel(job_id: str) -> Response:
        mesh.cancel_job(job_id, viewer().id)
        return jsonify({"ok": True})

    @app.delete("/api/send/<job_id>/targets/<target_id>")
    def ui_send_cancel_target(job_id: str, target_id: str) -> Response:
        mesh.cancel_target(job_id, viewer(), target_id)
        return jsonify({"ok": True})

    @app.post("/api/signal")
    def ui_signal() -> Response:
        mesh.signal(viewer(), body_json())
        return jsonify({"ok": True})

    @app.get("/api/signals")
    def ui_signals() -> Response:
        return jsonify({"signals": mesh.take_signals(viewer())})

    @app.post("/api/send/<job_id>/targets/<target_id>/direct")
    def ui_send_direct(job_id: str, target_id: str) -> Response:
        data = body_json()
        try:
            sent = int(data.get("sent") or 0)
        except (TypeError, ValueError) as exc:
            raise MeshError(400, "bad_request", "Invalid progress.") from exc
        if sent < 0:
            raise MeshError(400, "bad_request", "Invalid progress.")
        mesh.set_direct(job_id, viewer(), target_id, str(data.get("state", "")), sent)
        return jsonify({"ok": True})

    @app.post("/api/send/<job_id>/retry")
    def ui_send_retry(job_id: str) -> tuple[Response, int]:
        """Retry a finished job: re-offer its files to declined/failed targets.

        The sender's page re-uploads the file bytes (it still holds the File
        objects); this endpoint just creates the fresh offers so the receiver
        is asked again.
        """
        current = viewer()
        data = request.get_json(silent=True) or {}
        targets = data.get("to") if isinstance(data, dict) else None
        if targets is not None and not isinstance(targets, list):
            raise MeshError(400, "bad_request", "Invalid targets.")
        job = mesh.retry_job(job_id, current, list(targets) if targets is not None else None)
        return jsonify({"job": mesh._job_view(job)}), 201

    @app.post("/api/incoming/<session_id>/direct")
    def ui_incoming_direct(session_id: str) -> Response:
        data = body_json()
        try:
            index, received = int(data.get("index", -1)), int(data.get("received", 0))
        except (TypeError, ValueError) as exc:
            raise MeshError(400, "bad_request", "Invalid progress.") from exc
        mesh.direct_received(session_id, viewer(), index, received, bool(data.get("done")))
        return jsonify({"ok": True})

    @app.post("/api/incoming/<session_id>/<any(accept, decline):decision>")
    def ui_decide(session_id: str, decision: str) -> Response:
        current = viewer()
        session = mesh.decide(session_id, current, decision == "accept")
        log_name = log_safe(session.origin.get("name"))
        app.logger.debug("%s %s from %s", decision, session_id, log_name)
        return jsonify({"state": session.state})

    @app.delete("/api/incoming/<session_id>")
    def ui_incoming_cancel(session_id: str) -> Response:
        mesh.cancel_incoming(session_id, viewer())
        return jsonify({"ok": True})

    # --------------------------------------------------- visitor inbox files

    def visitor_inbox() -> Any:
        current = viewer()
        if current.is_owner:
            abort(404, "The owner’s files are in the main list.")
        return mesh.inbox(current.id)

    @app.get("/api/inbox/files/<path:name>")
    def ui_inbox_file(name: str) -> Response:
        store = visitor_inbox()
        return serve_file(store.resolve(name), inline=request.args.get("inline") == "1")

    @app.delete("/api/inbox/files/<path:name>")
    def ui_inbox_delete(name: str) -> Response:
        store = visitor_inbox()
        path = store.resolve(name)
        path.unlink(missing_ok=True)
        return jsonify({"ok": True})

    @app.get("/api/inbox/archive")
    def ui_inbox_archive() -> Response:
        store = visitor_inbox()
        paths = [store.resolve(f.name) for f in store.files()]
        if not paths:
            abort(404, "There are no files to download.")
        stamp = time.strftime("%Y-%m-%d %H.%M")
        response = Response(stream_with_context(zip_stream(paths)), mimetype="application/zip")
        response.headers["Content-Disposition"] = (
            'attachment; filename="open-transfer.zip"; '
            f"filename*=UTF-8''{quote(f'Open Transfer {stamp}.zip')}"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    # -------------------------------------------------------- owner: devices

    @app.post("/api/devices")
    def ui_add_device() -> Response:
        # Read the body before the owner check so Windows doesn't reset the
        # connection when we answer 403 without draining the request.
        data = body_json()
        owner_only()
        peer = mesh.connect(str(data.get("address", "")))
        return jsonify({"device": {"id": peer.id, "name": peer.name}})

    @app.post("/api/pair")
    def ui_pair() -> Response:
        # Read the body before the owner check (see above).
        data = body_json()
        owner_only()
        address = str(data.get("address") or "").strip() or None
        peer = mesh.pair_with(str(data.get("code", "")), address)
        return jsonify({"device": {"id": peer.id, "name": peer.name}})

    @app.post("/api/pair/new-code")
    def ui_new_code() -> Response:
        owner_only()
        mesh._rotate_code()
        return jsonify({"code": mesh.pair_code, "expires_in": mesh.pair_code_expires_in()})

    @app.delete("/api/pairs/<device_id>")
    def ui_unpair(device_id: str) -> Response:
        owner_only()
        if not mesh.unpair(device_id):
            raise MeshError(404, "not_found", "That device isn’t paired.")
        return jsonify({"ok": True})

    @app.get("/api/pair/qr.svg")
    def ui_pair_qr() -> Response:
        owner_only()
        url = f"{share_url()}/?pair={mesh.pair_code}"
        if config.pin:
            url += f"&pin={quote(config.pin)}"
        buffer = io.BytesIO()
        segno.make(url, error="m").save(
            buffer, kind="svg", scale=8, border=0, dark="#000", light=None
        )
        return Response(
            buffer.getvalue(), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"}
        )

    # ------------------------------------------------- one-time invite links
    #
    # A single-use URL (``/?invite=…``) that lets exactly one visitor in —
    # even past the PIN — then expires. The owner mints them from the
    # "Add device" sheet; each shows a countdown and dies after one use.

    @app.post("/api/invites")
    def ui_invite_create() -> Response:
        data = request.get_json(silent=True) or {}
        owner_only()
        try:
            ttl = float(data.get("ttl", 600))
        except (TypeError, ValueError):
            raise MeshError(400, "bad_request", "Invalid expiry.") from None
        ttl = min(max(ttl, 60), 3600)
        invite = mesh.create_invite(ttl=ttl, single_use=True)
        url = f"{share_url()}/?invite={invite['token']}"
        return jsonify({**invite, "url": url})

    @app.get("/api/invites")
    def ui_invite_info() -> Response:
        owner_only()
        return jsonify(mesh.invite_info())


def _clean_origin(origin: dict[str, Any], sender: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {
        "id": str(origin.get("id")) if valid_id(origin.get("id")) else str(sender.get("id", "")),
        "name": clean_name(origin.get("name")),
        "form": clean_form(origin.get("form")),
        "platform": clean_platform(origin.get("platform")),
    }
    if clean["id"] != sender.get("id"):
        clean["via_name"] = clean_name(sender.get("name"))
    return clean
