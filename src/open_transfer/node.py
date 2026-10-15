"""Run an Open Transfer device: HTTP server + nearby-device discovery.

Used by the CLI, the desktop app and the Android app::

    node = Node(config)
    port = node.start()        # serve on a background thread
    ...
    node.stop()

or ``node.bind(); node.start_mesh(); node.serve_forever()`` to block.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any

from open_transfer import __version__, network
from open_transfer.config import Config
from open_transfer.mesh import Mesh


class Node:
    def __init__(self, config: Config) -> None:
        from open_transfer.app import create_app

        self.config = config
        self.app = create_app(config)
        self.mesh: Mesh = self.app.extensions["open_transfer"]["mesh"]
        self.port = 0
        self._server: Any = None
        self._thread: threading.Thread | None = None

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def bind(self) -> int:
        """Pick a free port (falling back from the preferred one) and listen on it."""
        from cheroot import wsgi

        port = network.find_free_port(self.config.host, self.config.port)
        self.app.config["OT_PORT"] = port
        server = wsgi.Server(
            (self.config.host, port),
            self.app,
            numthreads=self.config.threads,
            server_name=f"open-transfer/{__version__}",
            timeout=60,
        )
        server.max_request_body_size = 0  # limits are enforced by the app while streaming
        server.prepare()
        self._server = server
        self.port = port
        return port

    def start_mesh(self) -> None:
        self.mesh.start(self.port)

    def serve_forever(self) -> None:
        try:
            self._server.serve()
        finally:
            self.stop()

    def start(self) -> int:
        """Listen, serve on a background thread and start discovery. Returns the port."""
        if self._server is None:
            self.bind()
        self._thread = threading.Thread(target=self._server.serve, name="ot-http", daemon=True)
        self._thread.start()
        self.start_mesh()
        return self.port

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.mesh.stop()
        if self._server is not None:
            with contextlib.suppress(Exception):
                self._server.stop()
