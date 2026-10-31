"""Pairing keys and the browser sign-in key at rest (docs/encryption.md, "Keys at rest")."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from open_transfer import keystore
from open_transfer.devices import TrustStore
from open_transfer.keystore import FileBackend, Locked, Vault

KEY = bytes(range(32))


class Memory:
    """An OS keystore stand-in: a dict, with switches to make it fail."""

    name = "memory"
    protected = True

    def __init__(self, items: dict[str, bytes] | None = None) -> None:
        self.items = {} if items is None else items
        self.fail_put = False
        self.forgets = False  # put "works" but nothing can be read back

    def get(self, kid: str) -> bytes | None:
        return None if self.forgets else self.items.get(kid)

    def put(self, kid: str, key: bytes) -> None:
        if self.fail_put:
            raise OSError("keystore unavailable")
        self.items[kid] = key

    def delete(self, kid: str) -> None:
        self.items.pop(kid, None)


def everything_in(state: Path) -> bytes:
    return b"".join(p.read_bytes() for p in state.rglob("*") if p.is_file())


def test_sealed_values_open_only_for_what_they_were_sealed_for(tmp_path: Path) -> None:
    vault = Vault(tmp_path, Memory())
    sealed = vault.seal(KEY, "pair|d-aaaaaaaa")
    assert KEY.hex() not in sealed
    opened = vault.open(sealed, "pair|d-aaaaaaaa")
    assert opened == KEY
    with pytest.raises(ValueError, match="damaged"):
        vault.open(sealed, "pair|d-bbbbbbbb")  # moved onto another device's entry
    with pytest.raises(ValueError, match="damaged"):
        vault.open(
            sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB"), "pair|d-aaaaaaaa"
        )


def test_the_vault_key_lives_in_the_keystore_not_the_state_folder(tmp_path: Path) -> None:
    store = Memory()
    vault = Vault(tmp_path, store)
    assert vault.store == "memory"
    assert vault.protected
    assert len(store.items) == 1
    (vault_key,) = store.items.values()
    assert vault_key not in everything_in(tmp_path)


def test_restart_reuses_the_vault_key(tmp_path: Path) -> None:
    store = Memory()
    sealed = Vault(tmp_path, store).seal(KEY, "session")
    again = Vault(tmp_path, store)
    opened = again.open(sealed, "session")
    assert opened == KEY
    assert len(store.items) == 1


def test_pair_keys_are_never_written_in_plain(tmp_path: Path) -> None:
    store = Memory()
    TrustStore(tmp_path, Vault(tmp_path, store)).add("d-aaaaaaaa", "Phone", KEY)
    assert KEY.hex().encode() not in everything_in(tmp_path)
    reloaded = TrustStore(tmp_path, Vault(tmp_path, store)).get("d-aaaaaaaa")
    assert reloaded is not None
    assert reloaded.key == KEY.hex()


def test_old_trusted_json_is_migrated_without_losing_a_pairing(tmp_path: Path) -> None:
    legacy = {
        "d-aaaaaaaa": {"name": "Phone", "key": KEY.hex(), "paired_at": 5.0, "enc": True},
        "d-bbbbbbbb": {"name": "PC", "key": "11" * 32, "paired_at": 6.0, "platform": "windows"},
    }
    (tmp_path / "trusted.json").write_text(json.dumps(legacy))
    store = Memory()
    trust = TrustStore(tmp_path, Vault(tmp_path, store))
    assert {d.id: d.key for d in trust.all()} == {"d-aaaaaaaa": KEY.hex(), "d-bbbbbbbb": "11" * 32}
    # Rewritten at once, sealed; everything else kept.
    saved = json.loads((tmp_path / "trusted.json").read_text())
    assert saved["d-aaaaaaaa"]["key"].startswith("ot-vault/1:")
    assert saved["d-aaaaaaaa"]["enc"] is True
    assert saved["d-bbbbbbbb"]["platform"] == "windows"
    assert KEY.hex().encode() not in everything_in(tmp_path)
    phone = TrustStore(tmp_path, Vault(tmp_path, store)).get("d-aaaaaaaa")
    assert phone is not None
    assert phone.enc


def test_a_missing_keystore_item_locks_pairings_but_keeps_them(tmp_path: Path) -> None:
    store = Memory()
    TrustStore(tmp_path, Vault(tmp_path, store)).add("d-aaaaaaaa", "Phone", KEY)

    # Keychain reset, access denied, the folder copied to another computer...
    gone = dict(store.items)
    store.items.clear()
    trust = TrustStore(tmp_path, Vault(tmp_path, store))
    assert trust.get("d-aaaaaaaa") is None  # never trusted with a key it doesn't have
    assert trust.locked_names() == ["Phone"]
    trust.add("d-bbbbbbbb", "PC", b"\x22" * 32)  # life goes on...
    assert "d-aaaaaaaa" in json.loads((tmp_path / "trusted.json").read_text())  # ...nothing lost

    # The old key comes back: so does the pairing.
    store.items.update(gone)
    back = TrustStore(tmp_path, Vault(tmp_path, store))
    assert {d.name for d in back.all()} == {"Phone", "PC"}
    assert back.locked_names() == []


def test_a_damaged_entry_is_locked_not_dropped(tmp_path: Path) -> None:
    store = Memory()
    TrustStore(tmp_path, Vault(tmp_path, store)).add("d-aaaaaaaa", "Phone", KEY)
    data = json.loads((tmp_path / "trusted.json").read_text())
    data["d-aaaaaaaa"]["key"] = data["d-aaaaaaaa"]["key"][:-6] + "AAAAAA"
    (tmp_path / "trusted.json").write_text(json.dumps(data))
    trust = TrustStore(tmp_path, Vault(tmp_path, store))
    assert trust.get("d-aaaaaaaa") is None
    assert trust.locked_names() == ["Phone"]


def test_pairing_again_replaces_a_locked_entry(tmp_path: Path) -> None:
    TrustStore(tmp_path, Vault(tmp_path, Memory())).add("d-aaaaaaaa", "Phone", KEY)
    store = Memory()
    trust = TrustStore(tmp_path, Vault(tmp_path, store))
    trust.add("d-aaaaaaaa", "Phone", b"\x33" * 32)
    assert trust.locked_names() == []
    phone = TrustStore(tmp_path, Vault(tmp_path, store)).get("d-aaaaaaaa")
    assert phone is not None
    assert phone.key == "33" * 32


def test_unpairing_removes_the_sealed_key_even_when_locked(tmp_path: Path) -> None:
    store = Memory()
    trust = TrustStore(tmp_path, Vault(tmp_path, store))
    trust.add("d-aaaaaaaa", "Phone", KEY)
    trust.add("d-bbbbbbbb", "PC", b"\x22" * 32)
    removed = trust.remove("d-aaaaaaaa")
    assert removed
    locked = TrustStore(tmp_path, Vault(tmp_path, Memory()))
    removed = locked.remove("d-bbbbbbbb")
    assert removed
    assert json.loads((tmp_path / "trusted.json").read_text()) == {}


def test_a_keystore_that_refuses_falls_back_to_a_file_and_says_so(tmp_path: Path) -> None:
    store = Memory()
    store.fail_put = True
    vault = Vault(tmp_path, store)
    assert vault.store == "file"
    assert not vault.protected
    sealed = vault.seal(KEY, "session")
    # Next start the keystore still refuses: the file key still opens what it sealed.
    opened = Vault(tmp_path, store).open(sealed, "session")
    assert opened == KEY


def test_a_keystore_that_cannot_read_back_is_not_trusted(tmp_path: Path) -> None:
    store = Memory()
    store.forgets = True
    vault = Vault(tmp_path, store)
    assert vault.store == "file"
    assert not vault.protected


def test_moving_to_the_keystore_when_it_appears(tmp_path: Path) -> None:
    refusing = Memory()
    refusing.fail_put = True
    TrustStore(tmp_path, Vault(tmp_path, refusing)).add("d-aaaaaaaa", "Phone", KEY)
    store = Memory()
    trust = TrustStore(tmp_path, Vault(tmp_path, store))  # e.g. the keyring is unlocked now
    assert trust.get("d-aaaaaaaa") is not None
    assert json.loads((tmp_path / "vault.json").read_text())["store"] == "memory"
    sealed = json.loads((tmp_path / "trusted.json").read_text())["d-aaaaaaaa"]["key"]
    assert sealed.startswith("ot-vault/1:memory:")


def test_rotating_the_vault_key(tmp_path: Path) -> None:
    store = Memory()
    keystore.rotate(tmp_path, Vault(tmp_path, store))  # nothing to reseal yet: fine
    TrustStore(tmp_path, Vault(tmp_path, store)).add("d-aaaaaaaa", "Phone", KEY)
    keystore.session_secret(tmp_path, Vault(tmp_path, store))
    secret_before = keystore.session_secret(tmp_path, Vault(tmp_path, store))
    (old,) = store.items
    keystore.rotate(tmp_path, Vault(tmp_path, store))
    assert old not in store.items
    assert len(store.items) == 1
    phone = TrustStore(tmp_path, Vault(tmp_path, store)).get("d-aaaaaaaa")
    assert phone is not None
    assert phone.key == KEY.hex()
    assert keystore.session_secret(tmp_path, Vault(tmp_path, store)) == secret_before


def test_rotating_keeps_locked_entries_and_their_key(tmp_path: Path) -> None:
    gone = Memory()
    TrustStore(tmp_path, Vault(tmp_path, gone)).add("d-aaaaaaaa", "Phone", KEY)
    store = Memory()
    keystore.rotate(tmp_path, Vault(tmp_path, store))
    assert "d-aaaaaaaa" in json.loads((tmp_path / "trusted.json").read_text())
    assert TrustStore(tmp_path, Vault(tmp_path, gone)).get("d-aaaaaaaa") is not None


def test_the_browser_sign_in_key_is_sealed_and_migrated(tmp_path: Path) -> None:
    legacy = os.urandom(32)
    (tmp_path / "secret").write_bytes(legacy)
    store = Memory()
    assert keystore.session_secret(tmp_path, Vault(tmp_path, store)) == legacy
    assert legacy not in everything_in(tmp_path)
    assert keystore.session_secret(tmp_path, Vault(tmp_path, store)) == legacy
    # Without its vault key a new one is made: browsers sign in again, nothing else breaks.
    fresh = keystore.session_secret(tmp_path, Vault(tmp_path, Memory()))
    assert len(fresh) == 32
    assert fresh != legacy


def test_the_key_never_shows_in_a_log_line(tmp_path: Path) -> None:
    trust = TrustStore(tmp_path, Vault(tmp_path, Memory()))
    device = trust.add("d-aaaaaaaa", "Phone", KEY)
    assert KEY.hex() not in repr(device)
    assert KEY.hex() not in str(trust.all())


def test_choosing_the_keystore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPEN_TRANSFER_KEYSTORE", raising=False)
    for platform, name in (
        ("macos", "macos-keychain"),
        ("windows", "windows-dpapi"),
        ("android", "android-keystore"),
    ):
        monkeypatch.setattr(keystore, "detect_platform", lambda p=platform: p)
        assert keystore.default_backend(tmp_path).name == name
    monkeypatch.setattr(keystore, "detect_platform", lambda: "linux")
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    assert keystore.default_backend(tmp_path).name == "file"  # e.g. Docker: no Secret Service
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/secret-tool")
    assert keystore.default_backend(tmp_path).name == "secret-service"
    monkeypatch.setenv("OPEN_TRANSFER_KEYSTORE", "file")
    monkeypatch.setattr(keystore, "detect_platform", lambda: "macos")
    assert keystore.default_backend(tmp_path).name == "file"


def test_the_file_fallback_is_owner_only(tmp_path: Path) -> None:
    backend = FileBackend(tmp_path)
    backend.put("k-0011223344556677", KEY)
    assert backend.get("k-0011223344556677") == KEY
    if os.name != "nt":
        (path,) = (tmp_path / "keys").iterdir()
        assert path.stat().st_mode & 0o077 == 0


def test_locked_and_unknown_kinds_are_told_apart(tmp_path: Path) -> None:
    vault = Vault(tmp_path, Memory())
    sealed = Vault(tmp_path / "other", Memory()).seal(KEY, "session")
    with pytest.raises(Locked):
        vault.open(sealed, "session")
    with pytest.raises(ValueError, match="not sealed"):
        vault.open("not a sealed value", "session")


# ----------------------------------------------------------- real keystores


def _round_trip(backend: keystore.Backend) -> None:
    kid = "k-" + os.urandom(8).hex()
    try:
        assert backend.get(kid) is None
        backend.put(kid, KEY)
        assert backend.get(kid) == KEY
    finally:
        backend.delete(kid)
    assert backend.get(kid) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain")
def test_macos_keychain() -> None:
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "open-transfer-test.keychain-db")
    subprocess.run(["security", "create-keychain", "-p", "test", path], check=True)  # noqa: S607
    try:
        subprocess.run(["security", "unlock-keychain", "-p", "test", path], check=True)  # noqa: S607
        backend = keystore.MacKeychain(path)
        assert backend.protected
        _round_trip(backend)
    finally:
        subprocess.run(["security", "delete-keychain", path], check=False)  # noqa: S607
        shutil.rmtree(folder, ignore_errors=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_windows_dpapi(tmp_path: Path) -> None:
    backend = keystore.WindowsDpapi(tmp_path)
    _round_trip(backend)
    backend.put("k-0011223344556677", KEY)
    assert KEY not in everything_in(tmp_path)


@pytest.mark.skipif(
    not os.environ.get("OT_TEST_SECRET_SERVICE"), reason="needs a running Secret Service"
)
def test_linux_secret_service() -> None:
    _round_trip(keystore.SecretService())


# ------------------------------------------------------------- what's shown


def test_the_owner_is_told_where_keys_are_and_which_are_locked(make_node, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from tests.test_mesh import owner
    from tests.test_moves import pair

    a, b = make_node("A"), make_node("B")
    pair(a, b)
    state = owner(a).state()
    assert state["keys"] == {"store": "file", "protected": False, "locked": []}
    trusted = (tmp_path / "A" / ".open-transfer" / "trusted.json").read_text()
    assert b.mesh.trust.get(a.mesh.id).key not in trusted  # type: ignore[union-attr]
    assert b.mesh.trust.get(a.mesh.id).key not in json.dumps(state)  # type: ignore[union-attr]


def test_cli_says_where_keys_are(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from open_transfer import cli

    assert cli.main([str(tmp_path), "--keys"]) == 0
    out = capsys.readouterr().out
    assert "a private file (no system keystore available)" in out
    assert "not protected by the operating system" in out
    assert cli.main([str(tmp_path), "--rotate-keys"]) == 0
    assert "New vault key" in capsys.readouterr().out
