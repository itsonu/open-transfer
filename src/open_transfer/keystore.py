"""Keeps pairing keys and the browser sign-in key out of plain files.

A random 32-byte *vault key* lives in the operating system's keystore; the
secrets in the state folder are sealed with it (AES-256-GCM, each bound to
what it is, so a sealed key can't be moved onto another device's entry)::

    ot-vault/1:<store>:<key id>:<base64 nonce + ciphertext>

=================  ===================================================
Store              Where the vault key is
=================  ===================================================
macos-keychain     the login Keychain (only this app may read it)
windows-dpapi      ``keys/<id>.dpapi``, encrypted to your Windows login
android-keystore   ``keys/<id>.wrapped``, wrapped by an Android Keystore
                   key that never leaves the phone's secure hardware
secret-service     GNOME Keyring / KWallet, through ``secret-tool``
file               ``keys/<id>``: owner-only, **not** OS-protected
                   (Docker, a Linux box without a keyring)
=================  ===================================================

Nothing is ever dropped because it can't be opened: an entry whose vault key
is missing (reinstall, keychain reset, the folder copied to another computer)
stays on disk *locked* until that key is back or the device is paired again.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import functools
import logging
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from open_transfer.devices import _read_json, detect_platform, write_private

log = logging.getLogger("open_transfer")

PREFIX = "ot-vault/1"
LABELS = {
    "macos-keychain": "the macOS Keychain",
    "windows-dpapi": "Windows DPAPI",
    "android-keystore": "the Android Keystore",
    "secret-service": "the Secret Service keyring",
    "file": "a private file (no system keystore available)",
}


class Locked(Exception):
    """The vault key that sealed this value isn't available."""


class Backend(Protocol):
    name: str
    protected: bool

    def get(self, kid: str) -> bytes | None: ...
    def put(self, kid: str, key: bytes) -> None: ...
    def delete(self, kid: str) -> None: ...


class FileBackend:
    """``<state>/keys/<id>``, owner-only. Subclasses wrap the key before it's written."""

    name = "file"
    protected = False
    suffix = ""

    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir / "keys"

    def _path(self, kid: str) -> Path:
        return self._dir / (kid + self.suffix)

    def _wrap(self, key: bytes) -> bytes:
        return key

    def _unwrap(self, blob: bytes) -> bytes:
        return blob

    def get(self, kid: str) -> bytes | None:
        try:
            blob = self._path(kid).read_bytes()
        except FileNotFoundError:
            return None
        return self._unwrap(blob)

    def put(self, kid: str, key: bytes) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path(kid), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(self._wrap(key))

    def delete(self, kid: str) -> None:
        self._path(kid).unlink(missing_ok=True)


class WindowsDpapi(FileBackend):
    """Encrypted with DPAPI: only this Windows user, on this PC, can decrypt it."""

    name = "windows-dpapi"
    protected = True
    suffix = ".dpapi"

    def _wrap(self, key: bytes) -> bytes:
        return _dpapi(key, protect=True)

    def _unwrap(self, blob: bytes) -> bytes:
        return _dpapi(blob, protect=False)


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char)))

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    entropy_bytes = b"open-transfer-vault/1"
    source = ctypes.create_string_buffer(data, len(data))
    extra = ctypes.create_string_buffer(entropy_bytes, len(entropy_bytes))
    blob_in = Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_char)))
    entropy = Blob(len(entropy_bytes), ctypes.cast(extra, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    ui_forbidden = 0x1
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    description = "Open Transfer vault key" if protect else None
    if not call(
        ctypes.byref(blob_in),
        description,
        ctypes.byref(entropy),
        None,
        None,
        ui_forbidden,
        ctypes.byref(blob_out),
    ):
        raise OSError(f"DPAPI error {kernel32.GetLastError()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class AndroidKeystore(FileBackend):
    """Wrapped by an AES key in the Android Keystore (``KeyVault.kt``), which can't be exported."""

    name = "android-keystore"
    protected = True
    suffix = ".wrapped"

    @staticmethod
    def _vault() -> Any:
        from java import jclass  # type: ignore[import-not-found]  # Chaquopy

        return jclass("io.github.itsonu.opentransfer.KeyVault")

    def _wrap(self, key: bytes) -> bytes:
        return str(self._vault().wrap(base64.b64encode(key).decode())).encode()

    def _unwrap(self, blob: bytes) -> bytes:
        return base64.b64decode(str(self._vault().unwrap(blob.decode())))


class MacKeychain:
    """A generic password in the login Keychain (or ``keychain``, a path, for tests).

    Items are made with the Keychain's default access: only the app that made
    them reads them without asking. An ad-hoc signed build is a "new app" after
    each update, so macOS asks once ("Always Allow"); a Developer ID build won't.
    """

    name = "macos-keychain"
    protected = True
    service = b"Open Transfer"
    not_found = -25300

    def __init__(self, keychain: str | None = None) -> None:
        self._path = keychain

    @functools.cached_property
    def _sec(self) -> Any:
        import ctypes
        import ctypes.util

        sec = ctypes.CDLL(ctypes.util.find_library("Security"))
        sec.SecKeychainOpen.argtypes = (ctypes.c_char_p, ctypes.c_void_p)
        sec.SecKeychainAddGenericPassword.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_void_p,
        )
        sec.SecKeychainFindGenericPassword.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        sec.SecKeychainItemFreeContent.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        sec.SecKeychainItemDelete.argtypes = (ctypes.c_void_p,)
        return sec

    def _keychain(self) -> Any:
        import ctypes

        if self._path is None:
            return None  # the default search list
        ref = ctypes.c_void_p()
        self._check(self._sec.SecKeychainOpen(self._path.encode(), ctypes.byref(ref)))
        return ref

    @staticmethod
    def _check(status: int) -> None:
        if status:
            raise OSError(f"Keychain error {status}")

    def _find(self, kid: str, *, want_data: bool) -> tuple[bytes | None, Any]:
        import ctypes

        account = kid.encode()
        length, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
        status = self._sec.SecKeychainFindGenericPassword(
            self._keychain(),
            len(self.service),
            self.service,
            len(account),
            account,
            ctypes.byref(length) if want_data else None,
            ctypes.byref(data) if want_data else None,
            ctypes.byref(item),
        )
        if status == self.not_found:
            return None, None
        self._check(status)
        value = None
        if want_data:
            value = ctypes.string_at(data, length.value)
            self._sec.SecKeychainItemFreeContent(None, data)
        return value, item

    def get(self, kid: str) -> bytes | None:
        value, item = self._find(kid, want_data=True)
        if item is not None:
            _cf_release(item)
        return value

    def put(self, kid: str, key: bytes) -> None:
        account = kid.encode()
        self._check(
            self._sec.SecKeychainAddGenericPassword(
                self._keychain(),
                len(self.service),
                self.service,
                len(account),
                account,
                len(key),
                key,
                None,
            )
        )

    def delete(self, kid: str) -> None:
        _, item = self._find(kid, want_data=False)
        if item is not None:
            try:
                self._check(self._sec.SecKeychainItemDelete(item))
            finally:
                _cf_release(item)


def _cf_release(ref: object) -> None:
    import ctypes
    import ctypes.util

    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    cf.CFRelease.argtypes = (ctypes.c_void_p,)
    cf.CFRelease(ref)


class SecretService:
    """GNOME Keyring / KWallet through ``secret-tool`` (the key goes over stdin, never argv)."""

    name = "secret-service"
    protected = True

    def _run(self, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603
            ["secret-tool", *args],  # noqa: S607
            input=data,
            capture_output=True,
            timeout=10,  # a locked keyring may wait for a password nobody types
            check=False,
        )

    def _attrs(self, kid: str) -> tuple[str, ...]:
        return ("application", "open-transfer", "kid", kid)

    def get(self, kid: str) -> bytes | None:
        done = self._run("lookup", *self._attrs(kid))
        out = done.stdout.strip()
        return bytes.fromhex(out.decode()) if done.returncode == 0 and out else None

    def put(self, kid: str, key: bytes) -> None:
        done = self._run(
            "store", "--label=Open Transfer vault key", *self._attrs(kid), data=key.hex().encode()
        )
        if done.returncode:
            raise OSError(done.stderr.decode(errors="replace").strip() or "secret-tool failed")

    def delete(self, kid: str) -> None:
        self._run("clear", *self._attrs(kid))


def default_backend(state_dir: Path) -> Backend:
    """This platform's keystore (``OPEN_TRANSFER_KEYSTORE=file`` opts out, e.g. for tests)."""
    if os.environ.get("OPEN_TRANSFER_KEYSTORE") == "file":
        return FileBackend(state_dir)
    platform = detect_platform()
    if platform == "android":
        return AndroidKeystore(state_dir)
    if platform == "windows":
        return WindowsDpapi(state_dir)
    if platform == "macos":
        return MacKeychain()
    if shutil.which("secret-tool"):
        return SecretService()
    return FileBackend(state_dir)


def _named(name: str, state_dir: Path) -> Backend | None:
    """The store a value says it was sealed with (to open what an earlier run sealed)."""
    makers: dict[str, type] = {
        "file": FileBackend,
        "windows-dpapi": WindowsDpapi,
        "android-keystore": AndroidKeystore,
    }
    if name in makers:
        return makers[name](state_dir)  # type: ignore[no-any-return]
    if name == "macos-keychain":
        return MacKeychain()
    if name == "secret-service":
        return SecretService()
    return None


class Vault:
    """Seals small secrets with the vault key; see the module docstring."""

    def __init__(self, state_dir: Path, backend: Backend | None = None) -> None:
        self._state = state_dir
        self._meta = state_dir / "vault.json"
        self._keys: dict[tuple[str, str], bytes] = {}
        self._backends: dict[str, Backend] = {}
        self.backend: Backend = backend or default_backend(state_dir)
        self.kid = ""
        meta = _read_json(self._meta)
        kid = str(meta.get("kid", ""))
        if meta.get("store") == self.backend.name and self._key(self.backend.name, kid):
            self.kid = kid
        elif meta.get("store") == self.backend.name:
            log.warning(
                "The key that unlocks paired devices isn't in %s any more; "
                "they stay locked until it's back, or until you pair them again.",
                LABELS.get(self.backend.name, self.backend.name),
            )
        if not self.kid:
            self._create(fallback=(str(meta.get("store", "")), kid))
        log.info("Pairing keys are kept in %s", LABELS.get(self.store, self.store))

    @property
    def store(self) -> str:
        return self.backend.name

    @property
    def protected(self) -> bool:
        return self.backend.protected

    def _create(self, fallback: tuple[str, str] = ("", "")) -> None:
        """A new vault key in the keystore; a file if the keystore can't keep it."""
        kid, key = "k-" + secrets.token_hex(8), secrets.token_bytes(32)
        try:
            self.backend.put(kid, key)
            if self.backend.get(kid) != key:
                raise OSError("it didn't keep the key")
        except (OSError, subprocess.SubprocessError, ValueError, ImportError) as exc:
            if self.backend.name != "file":
                log.warning(
                    "Couldn't use %s (%s); keeping pairing keys in a private file instead.",
                    LABELS.get(self.backend.name, self.backend.name),
                    exc,
                )
                self.backend = self._backend("file")  # type: ignore[assignment]
                if fallback[0] == "file" and self._key("file", fallback[1]):
                    self.kid = fallback[1]  # the keystore refused last time too: same file key
                    return
                with contextlib.suppress(OSError):
                    self.backend.put(kid, key)
        self._keys[(self.backend.name, kid)] = key
        self.kid = kid
        try:
            write_private(self._meta, {"store": self.backend.name, "kid": kid})
        except OSError:  # read-only state folder: this key lasts this run only
            log.debug("could not save the vault key id", exc_info=True)

    def _backend(self, name: str) -> Backend | None:
        if name == self.backend.name:
            return self.backend
        if name not in self._backends:
            backend = _named(name, self._state)
            if backend is None:
                return None
            self._backends[name] = backend
        return self._backends[name]

    def _key(self, store: str, kid: str) -> bytes | None:
        if (store, kid) not in self._keys:
            backend = self._backend(store)
            try:
                key = backend.get(kid) if backend and kid else None
            except (OSError, subprocess.SubprocessError, ValueError, ImportError) as exc:
                log.warning("Couldn't read a key from %s: %s", LABELS.get(store, store), exc)
                key = None
            if key is None or len(key) != 32:
                return None
            self._keys[(store, kid)] = key
        return self._keys[(store, kid)]

    def seal(self, data: bytes, label: str) -> str:
        header = f"{PREFIX}:{self.store}:{self.kid}"
        nonce = secrets.token_bytes(12)
        key = self._keys[(self.store, self.kid)]
        sealed = AESGCM(key).encrypt(nonce, data, f"{header}|{label}".encode())
        return f"{header}:{base64.b64encode(nonce + sealed).decode()}"

    def open(self, sealed: str, label: str) -> bytes:
        try:
            version, store, kid, data = sealed.split(":", 3)
        except ValueError:
            raise ValueError("not sealed") from None
        if version != PREFIX:
            raise ValueError("not sealed")
        key = self._key(store, kid)
        if key is None:
            raise Locked(f"{store}:{kid}")
        try:
            raw = base64.b64decode(data, validate=True)
            aad = f"{version}:{store}:{kid}|{label}".encode()
            return AESGCM(key).decrypt(raw[:12], raw[12:], aad)
        except (binascii.Error, InvalidTag, ValueError):
            raise ValueError("damaged") from None

    @staticmethod
    def is_sealed(value: object) -> bool:
        return isinstance(value, str) and value.startswith(PREFIX + ":")

    def is_current(self, sealed: str) -> bool:
        return sealed.startswith(f"{PREFIX}:{self.store}:{self.kid}:")

    def rotate(self) -> tuple[str, str]:
        """Start sealing with a new vault key; returns the old one for :meth:`forget`."""
        old = (self.store, self.kid)
        self._create()
        return old

    def forget(self, old: tuple[str, str]) -> None:
        backend = self._backend(old[0])
        if backend is not None and old != (self.store, self.kid):
            backend.delete(old[1])
            self._keys.pop(old, None)


@functools.cache
def open_vault(state_dir: Path) -> Vault:
    """The vault for a state folder (one per process)."""
    return Vault(state_dir)


def session_secret(state_dir: Path, vault: Vault | None = None) -> bytes:
    """The key that signs browser sign-ins, kept across restarts so PIN logins survive them."""
    vault = vault or open_vault(state_dir)
    path = state_dir / "secret"
    try:
        stored = path.read_bytes()
    except OSError:
        stored = b""
    sealed = _read_json(path).get("key") if stored.startswith(b"{") else None
    secret, current = None, False
    if vault.is_sealed(sealed):
        try:
            secret = vault.open(str(sealed), "session")
            current = vault.is_current(str(sealed))
        except (Locked, ValueError):
            log.info("Browser sign-ins were reset: their key couldn't be unlocked")
    elif len(stored) >= 32:
        secret = stored  # from before the vault: sealed below
    if secret is None:
        secret = secrets.token_bytes(32)
    if not current:
        try:
            write_private(path, {"key": vault.seal(secret, "session")})
        except OSError:  # read-only volume: a per-process key
            log.debug("could not persist session secret", exc_info=True)
    return secret


def rotate(state_dir: Path, vault: Vault | None = None) -> None:
    """Reseal everything with a new vault key, then delete the old one (app not running)."""
    from open_transfer.devices import TrustStore

    vault = vault or open_vault(state_dir)
    old = vault.rotate()
    TrustStore(state_dir, vault)  # reseals what it can open; locked entries stay as they are
    session_secret(state_dir, vault)
    vault.forget(old)
