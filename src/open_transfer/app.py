"""The Flask application: HTML shell, JSON API and file serving."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import logging
import mimetypes
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import segno
from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    stream_with_context,
    url_for,
)
from werkzeug.exceptions import ClientDisconnected, HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from open_transfer import __version__, network
from open_transfer.archive import zip_stream
from open_transfer.config import Config
from open_transfer.mesh import Mesh, Viewer, visitor_identity
from open_transfer.mesh_api import PUBLIC_P2P_ENDPOINTS
from open_transfer.mesh_api import register as register_mesh_routes
from open_transfer.security import (
    SAFE_METHODS,
    RateLimiter,
    apply_security_headers,
    host_allowed,
    log_safe,
    pin_matches,
    same_origin,
)
from open_transfer.storage import IncompleteUpload, InvalidName, Storage, StorageError

log = logging.getLogger("open_transfer")

PACKAGE_DIR = Path(__file__).resolve().parent
INLINE_SAFE_MIME = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/avif",
    "image/bmp",
    "video/mp4",
    "video/webm",
    "audio/mpeg",
    "audio/mp4",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
}
FILE_CSP = (
    "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox"
)
PUBLIC_ENDPOINTS = {
    "static",
    "health",
    "manifest",
    "index",
    "legacy_page",
    "auth",
    "info",
    *PUBLIC_P2P_ENDPOINTS,
}


def _load_secret(state_dir: Path) -> bytes:
    """Keep the session key across restarts so PIN logins survive them."""
    path = state_dir / "secret"
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except OSError:
        log.debug("no stored session secret yet; creating one")
    data = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except OSError:  # read-only volume: fall back to a per-process key
        log.debug("could not persist session secret", exc_info=True)
    return data


def create_app(config: Config | None = None) -> Flask:
    config = config or Config()
    storage = Storage(
        config.storage_dir,
        reserve_bytes=config.reserve_disk_bytes,
        trash_ttl=config.trash_ttl,
    )
    mesh = Mesh(config, storage)
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.update(
        SECRET_KEY=_load_secret(config.state_path),
        SESSION_COOKIE_NAME="open_transfer",
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
        MAX_CONTENT_LENGTH=None,
        SEND_FILE_MAX_AGE_DEFAULT=None,
    )
    if config.trust_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]
    if (config.public_url or "").startswith("https://"):
        app.config["SESSION_COOKIE_SECURE"] = True
    app.json.sort_keys = False  # type: ignore[attr-defined]
    app.extensions["open_transfer"] = {"config": config, "storage": storage, "mesh": mesh}
    limiter = RateLimiter(attempts=5, window=60)
    machine_names = (network.hostname().lower(), f"{network.hostname().lower()}.local")
    pin_digest = hashlib.sha256((config.pin or "").encode()).hexdigest()[:16]

    # ------------------------------------------------------------------ helpers

    def wants_json() -> bool:
        return (
            request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"
        )

    def error(status: int, code: str, message: str, **extra: Any) -> Response:
        if wants_json():
            response = jsonify({"error": {"code": code, "message": message, **extra}})
        else:
            response = Response(
                render_template("error.html", status=status, message=message, code=code),
                mimetype="text/html",
            )
        response.status_code = status
        return response

    def is_owner() -> bool:
        """The person using this device: requests from the machine itself."""
        if not config.owner_loopback:
            return False
        try:
            return ipaddress.ip_address(request.remote_addr or "").is_loopback
        except ValueError:
            return False

    def is_authenticated() -> bool:
        return not config.pin or is_owner() or session.get("pin") == pin_digest

    def current_viewer() -> Viewer:
        if is_owner():
            ident = mesh.identity
            return Viewer("owner", ident.id, ident.name, ident.form, ident.platform, True)
        vid, name, form, plat = visitor_identity(
            session.get("wid"), session.get("wname"), session.get("wform"), session.get("wplat")
        )
        if session.get("wid") != vid:
            session.permanent = True
            session["wid"] = vid
        trusted = session.get("wtrust") == mesh.id
        mesh.touch_visitor(
            vid, name=name, form=form, platform=plat, address=client_id(), trusted=trusted
        )
        return Viewer("visitor", vid, name, form, plat, trusted)

    def can_browse() -> bool:
        return is_owner() or (config.share_folder and config.allow_browse)

    def can_upload() -> bool:
        return is_owner() or (config.share_folder and config.allow_upload)

    def can_delete() -> bool:
        return is_owner() or (config.share_folder and config.allow_browse and config.allow_delete)

    def request_host_port() -> tuple[str, int]:
        parts = urlsplit(f"//{request.host}")
        default = 443 if request.scheme == "https" else 80
        # The Host header's port is what the visitor actually used (it differs
        # from the listen port behind Docker port mapping or a proxy).
        return parts.hostname or "localhost", int(parts.port or default)

    def origin_for(host: str, port: int) -> str:
        scheme = request.scheme if request.scheme in {"http", "https"} else "http"
        if ":" in host:
            host = f"[{host}]"
        default = 443 if scheme == "https" else 80
        return f"{scheme}://{host}" if port == default else f"{scheme}://{host}:{port}"

    def share_url() -> str:
        if config.public_url:
            return config.public_url
        host, port = request_host_port()
        if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
            host = network.primary_ip() or host
        return origin_for(host, port)

    def all_urls() -> list[str]:
        urls = [share_url()]
        if not config.public_url:
            # LAN addresses reach the server directly, so use the listen port.
            port = int(app.config.get("OT_PORT") or request_host_port()[1])
            urls += [f"http://{ip}:{port}" for ip in network.lan_ips()]
        return list(dict.fromkeys(urls))

    def client_id() -> str:
        return log_safe(request.remote_addr or "unknown")

    def check_pin(supplied: str) -> float | bool:
        """``True`` if right, ``False`` if wrong, or seconds to wait if rate-limited."""
        if not config.pin:
            return True
        wait = limiter.attempt(client_id())
        if wait:
            return wait
        if pin_matches(config.pin, supplied):
            limiter.reset(client_id())
            session.permanent = True
            session["pin"] = pin_digest
            return True
        log.warning("Wrong PIN from %s", client_id())
        return False

    def server_info() -> dict[str, Any]:
        authed = is_authenticated()
        owner = is_owner()
        info: dict[str, Any] = {
            "app": "Open Transfer",
            "version": __version__,
            "device": mesh.identity.name,
            "owner": owner,
            "auth": {
                "required": bool(config.pin),
                "authenticated": authed,
                "numeric": bool(config.pin and config.pin.isdigit()),
            },
        }
        if authed:
            info.update(
                share_url=share_url(),
                urls=all_urls(),
                permissions={
                    "upload": can_upload(),
                    "browse": can_browse(),
                    "delete": can_delete() and can_browse(),
                    "send": owner or config.allow_upload,
                },
                shared_folder=config.share_folder,
                limits={"max_upload_size": config.max_upload_size},
                storage={"free": storage.usage()["free"]},
                pin=config.pin,
            )
        return info

    # ------------------------------------------------------------- middleware

    @app.before_request
    def guard() -> Response | None:
        g.started = time.perf_counter()
        if not host_allowed(request.host, config.allowed_hosts, machine_names):
            return error(
                421,
                "host_not_allowed",
                f"Requests for '{request.host}' are not accepted. "
                "Start Open Transfer with --allow-host to permit this name.",
            )
        if request.method not in SAFE_METHODS and not same_origin(request):
            return error(403, "cross_origin", "Cross-site requests are not allowed.")
        if request.endpoint not in PUBLIC_ENDPOINTS and not is_authenticated():
            return error(401, "pin_required", "Enter the PIN to continue.")
        return None

    @app.after_request
    def finish(response: Response) -> Response:
        apply_security_headers(response)
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.path.endswith((".js", ".css")):
            # ES module imports carry no version query; always revalidate (cheap 304s).
            response.headers["Cache-Control"] = "no-cache"
        if log.isEnabledFor(logging.DEBUG):
            elapsed = (time.perf_counter() - g.get("started", time.perf_counter())) * 1000
            log.debug(
                "%s %s %s %.0fms %s",
                log_safe(request.method),
                log_safe(request.full_path.rstrip("?")),
                response.status_code,
                elapsed,
                client_id(),
            )
        return response

    @app.errorhandler(StorageError)
    def storage_error(exc: StorageError) -> Response:
        return error(exc.status, exc.code, str(exc))

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException) -> Response:
        return error(
            exc.code or 500, (exc.name or "error").lower().replace(" ", "_"), exc.description or ""
        )

    @app.errorhandler(Exception)
    def unhandled(exc: Exception) -> Response:
        log.exception("Unhandled error on %s %s", log_safe(request.method), log_safe(request.path))
        return error(500, "internal_error", "Something went wrong on the sharing computer.")

    # ------------------------------------------------------------------ pages

    @app.get("/")
    def index() -> Any:
        supplied = request.args.get("pin")
        pair = request.args.get("pair")
        if supplied is not None or pair is not None:
            # QR codes embed the PIN (and the owner's pairing code) so scanning
            # is enough to get in. Strip them from the URL straight away so
            # they don't linger in history. Both are rate-limited.
            if supplied is not None and config.pin and not is_authenticated():
                check_pin(supplied)
            if (
                pair
                and not is_owner()
                and is_authenticated()
                and mesh.check_visitor_code(pair, client_id()) is True
            ):
                session.permanent = True
                session["wtrust"] = mesh.id
            return redirect(url_for("index"))
        return render_template("index.html", info=server_info(), version=__version__)

    @app.get("/upload")
    @app.get("/downloads")
    def legacy_page() -> Any:
        return redirect(url_for("index"), code=301)

    @app.get("/manifest.webmanifest")
    @app.get("/manifest.json")
    def manifest() -> Response:
        return send_from_directory(
            app.static_folder or "static",
            "manifest.webmanifest",
            mimetype="application/manifest+json",
            max_age=3600,
        )

    # -------------------------------------------------------------------- API

    @app.get("/api/health")
    def health() -> Response:
        return jsonify({"status": "ok", "version": __version__})

    @app.get("/api/info")
    def info() -> Response:
        return jsonify(server_info())

    @app.post("/api/auth")
    def auth() -> Response:
        if not config.pin:
            return jsonify({"authenticated": True})
        body = request.get_json(silent=True) or {}
        result = check_pin(str(body.get("pin", "")))
        if result is True:
            return jsonify({"authenticated": True})
        if result is False:
            return error(401, "wrong_pin", "That PIN isn't right.")
        wait = float(result)
        response = error(
            429,
            "too_many_attempts",
            "Too many wrong PINs. Try again shortly.",
            retry_after=round(wait),
        )
        response.headers["Retry-After"] = str(max(1, round(wait)))
        return response

    @app.post("/api/logout")
    def logout() -> Response:
        session.clear()
        return jsonify({"authenticated": False})

    def require_browse() -> None:
        if not can_browse():
            if config.share_folder:
                abort(403, "This computer only receives files. Browsing is turned off.")
            abort(403, "This device’s files are private. Ask its owner to send you what you need.")

    @app.get("/api/files")
    def list_files() -> Response:
        require_browse()
        files = storage.files()
        etag = storage.fingerprint(files)
        if request.if_none_match.contains(etag):
            return Response(status=304, headers={"ETag": f'"{etag}"'})
        response = jsonify(
            {
                "files": [f.to_dict() for f in files],
                "total_size": sum(f.size for f in files),
                "storage": {"free": storage.usage()["free"]},
            }
        )
        response.set_etag(etag)
        return response

    @app.post("/api/files")
    @app.post("/transfer")
    def upload() -> Any:
        if not can_upload():
            return error(403, "upload_disabled", "Uploading is turned off on this computer.")
        saved = []
        try:
            if request.mimetype == "multipart/form-data":
                uploads = request.files.getlist("file") or list(request.files.values())
                if not uploads:
                    return error(400, "no_file", "No file was sent.")
                for item in uploads:
                    if not item.filename:
                        continue
                    saved.append(
                        storage.save_stream(
                            item.filename, item.stream, max_size=config.max_upload_size
                        )
                    )
                if not saved:
                    return error(400, "no_file", "No file was selected.")
            else:
                if request.content_length is None and not request.environ.get(
                    "wsgi.input_terminated"
                ):
                    # Without a length or a server that de-chunks the body we
                    # can't tell a complete upload from an empty one.
                    return error(411, "length_required", "Send a Content-Length header.")
                # The header is percent-encoded by clients; the query string is
                # already decoded by Werkzeug, so don't decode it twice.
                header = request.headers.get("X-Filename")
                name = unquote(header) if header else request.args.get("name", "")
                if not name:
                    raise InvalidName("Missing file name (send an X-Filename header).")
                saved.append(
                    storage.save_stream(
                        name,
                        request.stream,
                        length=request.content_length,
                        max_size=config.max_upload_size,
                    )
                )
        except (ClientDisconnected, ConnectionError) as exc:
            raise IncompleteUpload("The upload was interrupted before it finished.") from exc
        for f in saved:
            log.info("Received %s (%s bytes) from %s", log_safe(f.name), f.size, client_id())
        return jsonify({"files": [f.to_dict() for f in saved]}), 201

    @app.delete("/api/files/<path:name>")
    def delete_file(name: str) -> Response:
        require_browse()
        if not can_delete():
            return error(403, "delete_disabled", "Deleting is turned off on this computer.")
        token = storage.delete(name)
        log.info("Deleted %s (by %s)", log_safe(name), client_id())
        return jsonify({"undo_token": token, "undo_seconds": config.trash_ttl})

    @app.post("/api/trash/<token>/restore")
    def restore_file(token: str) -> Response:
        require_browse()
        restored = storage.restore(token)
        log.info("Restored %s (by %s)", log_safe(restored.name), client_id())
        return jsonify({"file": restored.to_dict()})

    @app.get("/api/archive")
    def archive() -> Response:
        require_browse()
        names = request.args.getlist("name")
        paths = (
            [storage.resolve(n) for n in names]
            if names
            else [storage.resolve(f.name) for f in storage.files()]
        )
        if not paths:
            abort(404, "There are no files to download.")
        stamp = time.strftime("%Y-%m-%d %H.%M")
        filename = f"Open Transfer {stamp}.zip"
        response = Response(stream_with_context(zip_stream(paths)), mimetype="application/zip")
        response.headers["Content-Disposition"] = (
            f"attachment; filename=\"open-transfer.zip\"; filename*=UTF-8''{quote(filename)}"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/qr.svg")
    def qr_code() -> Response:
        url = share_url()
        if config.pin:
            url += f"/?pin={quote(config.pin)}"
        buffer = io.BytesIO()
        segno.make(url, error="m").save(
            buffer, kind="svg", scale=8, border=0, dark="#000", light=None
        )
        return Response(
            buffer.getvalue(), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"}
        )

    @app.get("/files/<path:name>")
    @app.get("/download/<path:name>")
    def download(name: str) -> Response:
        require_browse()
        path = storage.resolve(name)
        info_mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        inline = request.args.get("inline") == "1" and info_mime in INLINE_SAFE_MIME
        response = send_file(
            path,
            mimetype=info_mime,
            as_attachment=not inline,
            download_name=path.name,
            conditional=True,
            max_age=0,
        )
        response.headers["Content-Security-Policy"] = FILE_CSP
        if request.method == "GET" and not request.range and not inline:
            log.info("Sent %s to %s", log_safe(path.name), client_id())
        return response

    register_mesh_routes(
        app,
        mesh,
        viewer=current_viewer,
        client=client_id,
        file_csp=FILE_CSP,
        share_url=share_url,
    )
    return app
