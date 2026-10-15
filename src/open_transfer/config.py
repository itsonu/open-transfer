"""Runtime configuration.

Every option can be set from the command line or from an ``OPEN_TRANSFER_*``
environment variable (handy for Docker). CLI flags win over the environment.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "OPEN_TRANSFER_"

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?i?b?)?\s*$", re.IGNORECASE)
_SIZE_POWERS = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4}


def parse_size(value: str | int | None) -> int:
    """Parse ``"500M"``, ``"2GB"``, ``"4GiB"`` or a plain byte count. ``0`` means unlimited.

    Units are decimal like Finder and Explorer show them (``1G`` = 10^9 bytes);
    ``KiB``/``MiB``/``GiB`` are binary.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        if value < 0:
            raise ValueError("size must not be negative")
        return value
    match = _SIZE_RE.match(value)
    if not match:
        raise ValueError(f"invalid size: {value!r} (try 500M, 2G or a number of bytes)")
    number, unit = match.groups()
    unit = (unit or "").lower()
    base = 1024 if "i" in unit else 1000
    return int(float(number) * base ** _SIZE_POWERS[unit[:1] if unit[:1] != "b" else ""])


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(ENV_PREFIX + name, default)


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    storage_dir: Path = field(default_factory=lambda: Path("uploads"))
    host: str = "0.0.0.0"
    port: int = 5000
    #: Optional PIN required before anyone can see or send files.
    pin: str | None = None
    #: Maximum size of a single upload in bytes. 0 = unlimited (bounded by free disk).
    max_upload_size: int = 0
    #: Allow visitors to delete shared files.
    allow_delete: bool = True
    #: Allow visitors to upload files. Disable to only *serve* a folder.
    allow_upload: bool = True
    #: Allow visitors to see and download shared files. Disable for a "drop box".
    allow_browse: bool = True
    #: URL shown in the UI and QR code (useful behind Docker or a reverse proxy).
    public_url: str | None = None
    #: Extra Host header values to accept (DNS-rebinding protection). "*" disables the check.
    allowed_hosts: tuple[str, ...] = ()
    #: Trust X-Forwarded-* headers from one reverse proxy in front of the app.
    trust_proxy: bool = False
    #: Always keep this many bytes of disk free.
    reserve_disk_bytes: int = 256 * 1024**2
    #: Seconds a deleted file stays restorable ("Undo").
    trash_ttl: float = 30.0
    #: Parallel connections the server accepts (each transfer holds one).
    threads: int = 32

    # ---------------------------------------------------------- nearby devices
    #: Name other devices see. Defaults to this computer's host name.
    device_name: str | None = None
    #: "computer", "phone" or "tablet" — picks the icon other devices show.
    device_form: str = "computer"
    #: "windows", "macos", "linux", "android"… Detected when left empty.
    device_platform: str | None = None
    #: Where the device identity and paired devices are kept.
    #: Defaults to ``<storage_dir>/.open-transfer``.
    state_dir: Path | None = None
    #: Announce this device and find others on the local network (UDP multicast).
    discovery: bool = True
    discovery_port: int = 47823
    #: Devices to connect to directly, as ``host:port`` (for networks that block multicast).
    peers: tuple[str, ...] = ()
    #: Accept incoming files without asking (for headless servers and NAS boxes).
    auto_accept: bool = False
    #: Only accept files from paired devices (AirDrop's "Contacts only").
    paired_only: bool = False
    #: Let browsers that connect to this device see and download its folder
    #: (the classic shared-folder mode). Off by default: files are sent to
    #: chosen devices instead.
    share_folder: bool = False
    #: Treat requests from this machine (127.0.0.1) as the device's owner, who
    #: sees everything, needs no PIN and accepts incoming files.
    owner_loopback: bool = True

    def __post_init__(self) -> None:
        self.storage_dir = Path(self.storage_dir).expanduser().resolve()
        if self.state_dir is not None:
            self.state_dir = Path(self.state_dir).expanduser().resolve()
        if self.device_form not in {"computer", "phone", "tablet"}:
            raise ValueError("device form must be computer, phone or tablet")
        if not 1 <= self.discovery_port <= 65535:
            raise ValueError("discovery port must be between 1 and 65535")
        self.peers = tuple(p.strip() for p in self.peers if p.strip())
        if self.device_name is not None:
            self.device_name = self.device_name.strip() or None
        if self.pin is not None:
            self.pin = self.pin.strip() or None
        if self.pin is not None and not re.fullmatch(r"[0-9A-Za-z]{4,32}", self.pin):
            raise ValueError("PIN must be 4-32 letters or digits")
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if self.max_upload_size < 0:
            raise ValueError("max upload size must not be negative")
        self.allowed_hosts = tuple(h.strip().lower() for h in self.allowed_hosts if h.strip())
        if self.public_url:
            self.public_url = self.public_url.rstrip("/")

    @property
    def state_path(self) -> Path:
        """Folder for the device identity, paired devices and session key."""
        return self.state_dir or self.storage_dir / ".open-transfer"
