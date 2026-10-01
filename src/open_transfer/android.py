"""Entry points for the Android app, called from Kotlin through Chaquopy.

The Android app is a thin native shell (WebView, file picker, QR scanner,
foreground service) around this same Python package, so phones and tablets
run exactly the same server, discovery and transfer code as desktops::

    val py = Python.getInstance().getModule("open_transfer.android")
    val port = py.callAttr("start", downloadsDir, filesDir, deviceName, "phone", listener).toInt()
    webView.loadUrl("http://127.0.0.1:$port/")

``listener`` is any Java/Kotlin object with ``onEvent(name: String, json: String)``;
it's told about ``"offer"`` (someone wants to send files — show a
notification) and ``"received"`` (a file was saved — run the media scanner).
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
        )
        node = Node(config)
        if listener is not None:
            node.mesh.add_listener(lambda event, data: listener.onEvent(event, json.dumps(data)))
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
