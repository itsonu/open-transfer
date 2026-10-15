from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from open_transfer import devices, discovery
from open_transfer.mesh import MeshError, _clean_files, _parse_address


def test_identity_is_stable_and_renamable(tmp_path: Path) -> None:
    first = devices.IdentityStore(tmp_path, name=None, form="computer", platform=None)
    second = devices.IdentityStore(tmp_path, name=None, form="computer", platform=None)
    assert first.identity.id == second.identity.id
    assert devices.valid_id(first.identity.id)
    second.rename("  Sonu’s\nMacBook  ")
    assert (
        devices.IdentityStore(tmp_path, name=None, form="computer", platform=None).identity.name
        == "Sonu’s MacBook"
    )
    assert (
        devices.IdentityStore(
            tmp_path, name="Override", form="phone", platform="android"
        ).identity.name
        == "Override"
    )


def test_trust_store_persists_privately(tmp_path: Path) -> None:
    store = devices.TrustStore(tmp_path)
    store.add("d-" + "a" * 24, "Laptop", b"k" * 32)
    again = devices.TrustStore(tmp_path)
    assert again.get("d-" + "a" * 24).name == "Laptop"  # type: ignore[union-attr]
    if os.name != "nt":  # POSIX permissions; Windows ACLs keep it in the user's profile
        assert (tmp_path / "trusted.json").stat().st_mode & 0o077 == 0
    removed = again.remove("d-" + "a" * 24)
    assert removed
    assert devices.TrustStore(tmp_path).get("d-" + "a" * 24) is None


def test_corrupt_trust_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "trusted.json").write_text(json.dumps({"x": 1, "d-" + "b" * 24: {"key": "zz"}}))
    assert devices.TrustStore(tmp_path).all() == []


def test_signatures_verify_once() -> None:
    key = b"s" * 32
    header = devices.sign(key, "POST", "/api/p2p/v1/offers", "d-" + "1" * 24, b"{}")
    checker = devices.SignatureChecker()
    assert checker.verify(key, header, "POST", "/api/p2p/v1/offers", "d-" + "1" * 24, b"{}")
    assert not checker.verify(
        key, header, "POST", "/api/p2p/v1/offers", "d-" + "1" * 24, b"{}"
    )  # replay
    other = devices.sign(key, "POST", "/x", "d-" + "1" * 24, b"{}")
    assert not checker.verify(key, other, "POST", "/y", "d-" + "1" * 24, b"{}")
    assert not checker.verify(
        b"t" * 32, devices.sign(key, "GET", "/", "d-1", b""), "GET", "/", "d-1", b""
    )
    assert not checker.verify(key, "garbage", "GET", "/", "d-1", b"")


def test_pairing_proofs_bind_code_and_both_ids() -> None:
    args = ("a" * 32, "b" * 32, "d-" + "1" * 24, "d-" + "2" * 24)
    assert devices.pair_proof("123456", "b", *args) == devices.pair_proof("123456", "b", *args)
    assert devices.pair_proof("123456", "b", *args) != devices.pair_proof("123457", "b", *args)
    assert devices.pair_proof("123456", "a", *args) != devices.pair_proof("123456", "b", *args)
    assert devices.pair_key("123456", *args) != bytes.fromhex(
        devices.pair_proof("123456", "b", *args)
    )
    assert len(devices.new_pair_code()) == 6


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Pixel\x00 8", "Pixel 8"), ("", "Unnamed device"), ("x" * 99, "x" * 40), ("  a\tb  ", "a b")],
)
def test_clean_name(value: str, expected: str) -> None:
    assert devices.clean_name(value) == expected


def test_discovery_packets_round_trip_and_reject_junk() -> None:
    packet = {"type": "announce", "id": "d-" + "c" * 24, "name": "Mac", "port": 5000}
    assert discovery.decode(discovery.encode(packet)) == {"app": "open-transfer", "v": 1, **packet}
    assert discovery.decode(b"not json") is None
    assert (
        discovery.decode(json.dumps({"app": "other", "v": 1, "type": "announce"}).encode()) is None
    )
    assert discovery.decode(discovery.encode({**packet, "port": 0})) is None
    assert discovery.decode(discovery.encode({**packet, "type": "evil"})) is None
    assert discovery.decode(b"x" * 5000) is None
    with pytest.raises(ValueError, match="too large"):
        discovery.encode({"type": "announce", "pad": "x" * 2000})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("192.168.1.20:5001", ("192.168.1.20", 5001)),
        ("http://laptop.local:5002/", ("laptop.local", 5002)),
        ("10.0.0.5", ("10.0.0.5", 5000)),
    ],
)
def test_parse_address(text: str, expected: tuple[str, int]) -> None:
    assert _parse_address(text) == expected


@pytest.mark.parametrize("text", ["", ":5000", "host:abc", "host:99999", "user@host:1", "a:1:2"])
def test_parse_address_rejects(text: str) -> None:
    with pytest.raises(MeshError):
        _parse_address(text)


def test_offered_files_are_sanitised() -> None:
    files = _clean_files([{"name": "../../etc/passwd", "size": 3}, {"name": "ok.txt", "size": 0}])
    assert [f.name for f in files] == ["passwd", "ok.txt"]
    for bad in ([], [{"name": "a", "size": -1}], [{"name": "a", "size": True}], ["x"], None):
        with pytest.raises(MeshError):
            _clean_files(bad)
