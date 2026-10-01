"""PyInstaller entry point for the Open Transfer desktop app (windowed, no console)."""

import os
import threading

from open_transfer.desktop import main

if __name__ == "__main__":
    code = main()
    # Sharing has stopped and the window is gone. A worker thread that is still
    # finishing must not keep an invisible app (and its single-instance lock) alive.
    backstop = threading.Timer(10.0, os._exit, args=(code,))
    backstop.daemon = True
    backstop.start()
    raise SystemExit(code)
