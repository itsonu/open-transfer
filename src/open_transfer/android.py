"""Entry points for the Android app, called from Kotlin through Chaquopy.

The Android app is a thin native shell (WebView, file picker, QR scanner,
foreground service) around this same Python package, so phones and tablets
run exactly the same server, discovery and transfer code as desktops::

    val py = Python.getInstance().getModule("open_transfer.android")
    val port = py.callAttr("start", downloadsDir, filesDir, deviceName, "phone", listener).toInt()
    webView.loadUrl("http://127.0.0.1:$port/")

``listener`` is any Java/Kotlin object with ``onEvent(name: String, json: String)``;
it's told about ``"offer"`` (someone wants to send files — show a
notification), ``"received"`` (a file was saved — run the media scanner) and
``"clipboard"`` (a clip from another of your devices — put it on the
clipboard; Android allows that from the background).

Android only lets the app in front read the clipboard, so Kotlin reads it
(when the app comes to the front, from the Quick Settings tile, the share
sheet) and hands it to :func:`sync_copy`.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path
from typing import Any

from open_transfer.config import Config
from open_transfer.node import Node
from open_transfer.sync.protocol import SyncError

log = logging.getLogger("open_transfer")

_node: Node | None = None
_lock = threading.Lock()


def start(
    storage_dir: str,
    state_dir: str,
    name: str = "",
    form: str = "phone",
    listener: Any = None,
    port: int = 5000,
    discovery: bool = True,
) -> int:
    """Start the device (idempotent). Returns the HTTP port on 127.0.0.1."""
    global _node
    with _lock:
        if _node is not None:
            return _node.port
        _configure_logging()
        state = Path(state_dir)
        # The name the system gives us is only a default; the owner can rename
        # the device in the app, which is stored in device.json.
        stored = (state / "device.json").exists()
        config = Config(
            storage_dir=Path(storage_dir),
            state_dir=state,
            device_name=None if stored else (name or None),
            device_form=form if form in {"phone", "tablet"} else "phone",
            device_platform="android",
            port=port,
            reserve_disk_bytes=64 * 1024**2,
            threads=24,
            discovery=discovery,
        )
        node = Node(config)
        if listener is not None:
            node.mesh.add_listener(lambda event, data: listener.onEvent(event, json.dumps(data)))

            def write_clip(text: str, info: dict[str, Any]) -> bool:
                clip = {
                    "text": text,
                    "sensitive": bool(info.get("sensitive")),
                    "from": info.get("from", ""),
                }
                listener.onEvent("clipboard", json.dumps(clip))
                return True

            node.mesh.sync.set_writer(write_clip)
        node.start()
        log.info("Open Transfer is running on port %s as %s", node.port, node.mesh.identity.name)
        _node = node
        return node.port


def stop() -> None:
    global _node
    with _lock:
        if _node is not None:
            _node.stop()
            _node = None


def sync_state() -> str:
    """Whether Kotlin should read the clipboard when the app comes to the front (JSON)."""
    node = _node
    if node is None:
        return json.dumps({"active": False, "mode": "auto", "clipboard": False})
    sync = node.mesh.sync
    return json.dumps(
        {
            "active": sync.active() and not sync.paused(),
            "mode": sync.policy()["mode"],
            "clipboard": sync.can_write,
        }
    )


def sync_copy(text: str, manual: bool = False, confirm: bool = False) -> str:
    """Send ``text`` (what Kotlin read from the clipboard, or was shared) to your devices.

    ``manual``: the owner asked (tile, share sheet) rather than "the app came to
    the front". Returns the engine's answer as JSON, plus the devices' names.
    """
    node = _node
    if node is None:
        return json.dumps({"status": "skipped", "reason": "stopped"})
    mesh = node.mesh
    try:
        result = mesh.sync.copy(str(text), manual=bool(manual), confirm=bool(confirm))
    except SyncError as exc:
        return json.dumps({"status": "error", "reason": exc.code, "message": str(exc)})
    result["names"] = {
        key: [mesh.sync._name(device_id) for device_id in result.get(key, [])]
        for key in ("delivered", "queued", "refused")
    }
    return json.dumps(result)


def is_running() -> bool:
    return _node is not None


def port() -> int:
    return _node.port if _node is not None else 0


def _configure_logging() -> None:
    root = logging.getLogger("open_transfer")
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)  # Chaquopy forwards stderr to logcat
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        root.propagate = False
