"""``open-transfer`` command-line entry point."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import secrets
import sys
import threading
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from open_transfer import __version__, network
from open_transfer.config import Config, env, env_bool, parse_size

log = logging.getLogger("open_transfer")


class _Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self(text, "1")

    def dim(self, text: str) -> str:
        return self(text, "2")

    def blue(self, text: str) -> str:
        return self(text, "1;34")

    def green(self, text: str) -> str:
        return self(text, "32")

    def yellow(self, text: str) -> str:
        return self(text, "33")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="open-transfer",
        description="AirDrop for every device: send files to phones, tablets and computers on "
        "your network.",
        epilog="Every option can also be set with an OPEN_TRANSFER_<NAME> environment variable.",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=None,
        help="folder to share and save uploads to (default: ./uploads, or "
        "~/Downloads/Open Transfer for the standalone app; env: OPEN_TRANSFER_DIR)",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=None, help="port to listen on (default: 5000)"
    )
    parser.add_argument(
        "--host", default=None, help="interface to bind (default: 0.0.0.0 = every network)"
    )
    parser.add_argument(
        "--pin",
        nargs="?",
        const="auto",
        default=None,
        help="require a PIN to connect; pass no value to generate a random one",
    )
    parser.add_argument(
        "--max-size",
        default=None,
        help="largest allowed upload, e.g. 500M or 4G (default: no limit)",
    )
    parser.add_argument(
        "--receive-only",
        action="store_true",
        default=None,
        help="visitors can send files but not browse or download",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        default=None,
        help="visitors can download but not upload",
    )
    parser.add_argument(
        "--no-delete", action="store_true", default=None, help="visitors cannot delete files"
    )
    parser.add_argument(
        "--public-url", default=None, help="URL to advertise, e.g. behind Docker or a proxy"
    )
    parser.add_argument(
        "--allow-host",
        action="append",
        default=None,
        metavar="NAME",
        help="extra host name to accept (repeatable; '*' disables the check)",
    )
    parser.add_argument(
        "--behind-proxy",
        action="store_true",
        default=None,
        help="trust X-Forwarded-* headers from a reverse proxy (nginx, Caddy, Traefik)",
    )
    nearby = parser.add_argument_group("nearby devices")
    nearby.add_argument("--name", default=None, help="name other devices see (default: host name)")
    nearby.add_argument(
        "--form",
        choices=["computer", "phone", "tablet"],
        default=None,
        help="device type shown to others (default: computer)",
    )
    nearby.add_argument(
        "--peer",
        action="append",
        default=None,
        metavar="HOST:PORT",
        help="connect to another Open Transfer app directly (repeatable; for networks "
        "that block discovery)",
    )
    nearby.add_argument(
        "--no-discovery",
        action="store_true",
        default=None,
        help="don't announce this device or look for others",
    )
    nearby.add_argument(
        "--auto-accept",
        action="store_true",
        default=None,
        help="accept incoming files without asking (headless servers)",
    )
    nearby.add_argument(
        "--paired-only",
        action="store_true",
        default=None,
        help="only accept files from paired devices",
    )
    nearby.add_argument(
        "--share-folder",
        action="store_true",
        default=None,
        help="classic mode: anyone who opens the link can browse and download the folder",
    )
    parser.add_argument(
        "--no-browser", action="store_true", default=None, help="don't open a browser"
    )
    parser.add_argument("--no-qr", action="store_true", default=None, help="don't print a QR code")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every request")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _pick(cli_value: object, env_name: str, default: object = None) -> object:
    if cli_value is not None:
        return cli_value
    value = env(env_name)
    return value if value is not None else default


def is_frozen_app() -> bool:
    """True when running as the standalone executable built by PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def default_storage_dir() -> Path:
    """Where files go when no folder is given.

    From a checkout or ``pip install`` it's ``./uploads`` (predictable for
    developers and scripts). The double-clickable app starts in whatever
    directory the OS picks, so it uses ``~/Downloads/Open Transfer`` instead.
    """
    if not is_frozen_app():
        return Path("uploads")
    downloads = Path.home() / "Downloads"
    return (downloads if downloads.is_dir() else Path.home()) / "Open Transfer"


def config_from_args(args: argparse.Namespace) -> Config:
    pin = _pick(args.pin, "PIN")
    if pin == "auto":
        pin = f"{secrets.randbelow(10**4):04d}"
    allowed = args.allow_host or [h for h in (env("ALLOWED_HOSTS") or "").split(",") if h]
    read_only = bool(args.read_only) or env_bool("READ_ONLY")
    return Config(
        storage_dir=Path(str(_pick(args.directory, "DIR", default_storage_dir()))),
        host=str(_pick(args.host, "HOST", "0.0.0.0")),
        port=int(str(_pick(args.port, "PORT", 5000))),
        pin=str(pin) if pin else None,
        max_upload_size=parse_size(str(_pick(args.max_size, "MAX_SIZE", "0"))),
        allow_upload=not read_only,
        allow_delete=not (read_only or bool(args.no_delete) or env_bool("NO_DELETE")),
        allow_browse=not (bool(args.receive_only) or env_bool("RECEIVE_ONLY")),
        public_url=_pick(args.public_url, "PUBLIC_URL"),  # type: ignore[arg-type]
        allowed_hosts=tuple(allowed),
        trust_proxy=bool(args.behind_proxy) or env_bool("BEHIND_PROXY"),
        device_name=_pick(args.name, "NAME"),  # type: ignore[arg-type]
        device_form=str(_pick(args.form, "FORM", "computer")),
        state_dir=Path(str(env("STATE_DIR"))) if env("STATE_DIR") else None,
        discovery=not (bool(args.no_discovery) or env_bool("NO_DISCOVERY")),
        discovery_port=int(str(env("DISCOVERY_PORT") or 47823)),
        peers=tuple(args.peer or [p for p in (env("PEERS") or "").split(",") if p]),
        auto_accept=bool(args.auto_accept) or env_bool("AUTO_ACCEPT"),
        paired_only=bool(args.paired_only) or env_bool("PAIRED_ONLY"),
        share_folder=bool(args.share_folder) or env_bool("SHARE_FOLDER"),
        owner_loopback=env_bool("OWNER_LOOPBACK", True),
    )


def _print_banner(
    config: Config,
    port: int,
    style: _Style,
    show_qr: bool,
    name: str = "",
    discovery: bool = True,
) -> str:
    name = name or network.hostname()
    ips = network.lan_ips()
    share = config.public_url or (f"http://{ips[0]}:{port}" if ips else f"http://localhost:{port}")
    out = sys.stdout
    out.write("\n  " + style.blue("Open Transfer") + style.dim(f"  v{__version__}") + "\n\n")
    out.write(f"  {style.dim('This device')}        {style.bold(name)}")
    out.write(style.dim("  · visible to nearby devices\n" if discovery else "  · discovery off\n"))
    out.write(f"  {style.dim('On this computer')}   http://localhost:{port}\n")
    if config.public_url:
        out.write(f"  {style.dim('Public address')}     {style.bold(config.public_url)}\n")
    for ip in ips:
        out.write(f"  {style.dim('On your network')}    {style.bold(f'http://{ip}:{port}')}\n")
    if not ips and not config.public_url:
        out.write(style.yellow("  No network connection found — only this computer can connect.\n"))
    if config.pin:
        out.write(f"  {style.dim('PIN')}                {style.bold(config.pin)}\n")
    out.write(f"  {style.dim('Saving files to')}    {config.storage_dir}\n")
    if show_qr and (ips or config.public_url):
        qr_url = share + (f"/?pin={config.pin}" if config.pin else "")
        try:
            import segno

            out.write("\n  " + style.dim("Scan with your phone's camera:") + "\n\n")
            segno.make(qr_url, error="l").terminal(out=out, compact=True, border=2)
        except (UnicodeEncodeError, OSError):
            # Some Windows consoles can't draw block characters; the URL above is enough.
            log.debug("could not print the QR code", exc_info=True)
    out.write("\n  " + style.dim("Press Ctrl+C to stop sharing.") + "\n\n")
    out.flush()
    return share


def _configure_logging(verbose: bool, style: _Style) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(style.dim("  %(asctime)s") + "  %(message)s", "%H:%M:%S")
    )
    root = logging.getLogger("open_transfer")
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.propagate = False


def main(argv: Sequence[str] | None = None) -> int:
    code = _run(argv)
    if code and is_frozen_app() and sys.stdin and sys.stdin.isatty():
        # A double-clicked app's window would vanish before the error could be read.
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            input("\n  Press Enter to close this window.")
    return code


def _run(argv: Sequence[str] | None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    style = _Style(sys.stdout.isatty() and "NO_COLOR" not in os.environ)
    _configure_logging(args.verbose, style)

    try:
        config = config_from_args(args)
    except ValueError as exc:
        print(f"open-transfer: error: {exc}", file=sys.stderr)
        return 2

    from open_transfer.instance import InstanceLock
    from open_transfer.node import Node

    # Two copies on one state folder would announce the same device id.
    lock = InstanceLock(config.state_path)
    if not lock.acquire():
        running = lock.read_info()
        where = f" at http://127.0.0.1:{running.port}" if running else ""
        print(
            f"open-transfer: error: Open Transfer is already running for this folder{where}.",
            file=sys.stderr,
        )
        return 1
    try:
        node = Node(config)
    except OSError as exc:
        print(f"open-transfer: error: cannot use {config.storage_dir}: {exc}", file=sys.stderr)
        return 1
    try:
        port = node.bind()
    except OSError as exc:
        print(f"open-transfer: error: could not listen: {exc}", file=sys.stderr)
        return 1
    if port != config.port and config.port:
        print(style.yellow(f"\n  Port {config.port} is busy, using {port} instead."))
    node.start_mesh()

    _print_banner(
        config,
        port,
        style,
        show_qr=not (args.no_qr or env_bool("NO_QR")),
        name=node.mesh.identity.name,
        discovery=bool(node.mesh.discovery and node.mesh.discovery.working),
    )
    if not (args.no_browser or env_bool("NO_BROWSER")):
        threading.Timer(0.4, webbrowser.open, args=(f"http://localhost:{port}",)).start()

    lock.write_info(port)
    with contextlib.suppress(KeyboardInterrupt):  # Ctrl+C is the normal way to stop
        node.serve_forever()
    lock.release()
    print("\n  " + style.dim("Stopped sharing. Your files are still in ") + str(config.storage_dir))
    return 0
