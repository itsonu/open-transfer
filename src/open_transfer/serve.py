"""Sending stored files to a browser: as a download, or inline for a preview."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from flask import Response, send_file

# Shown inline (in an <img>, <video> or <audio>, or the browser's viewer):
# types that can't run script in our origin.
INLINE_SAFE_MIME = frozenset(
    {
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
)
# Shown in a frame inside the app's preview: the browser's own PDF viewer.
INLINE_FRAMED_MIME = frozenset({"application/pdf"})

FILE_CSP = (
    "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox"
)
# The PDF viewer is a plugin (object-src) and can't run sandboxed; it may
# only be framed by the app itself.
PDF_CSP = "default-src 'none'; object-src 'self'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'"


def serve_file(path: Path, *, inline: bool = False) -> Response:
    """``path`` as a download, or inline when asked for and safe to show."""
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    framed = inline and mime in INLINE_FRAMED_MIME
    inline = inline and (mime in INLINE_SAFE_MIME or framed)
    response = send_file(
        path,
        mimetype=mime,
        as_attachment=not inline,
        download_name=path.name,
        conditional=True,
        max_age=0,
    )
    if framed:
        response.headers["Content-Security-Policy"] = PDF_CSP
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        response.headers["Content-Security-Policy"] = FILE_CSP
    return response
