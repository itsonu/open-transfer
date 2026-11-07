"""ot-file/1: per-file keys and AES-256-GCM STREAM records (docs/encryption.md)."""

from __future__ import annotations

import io
import os
import tracemalloc

import pytest

from open_transfer import filecrypt
from open_transfer.filecrypt import RECORD, TAG, Cipher, DecryptError, Decrypting, Encrypting

KEY = bytes(range(32))
SALT = bytes.fromhex(filecrypt.new_salt())


def cipher(size: int, *, index: int = 0, target: str = "d-bob", salt: bytes = SALT) -> Cipher:
    return Cipher(filecrypt.file_key(KEY, salt, "d-alice", "d-bob", target, index, size), size)


def seal(data: bytes, **kw: object) -> bytes:
    return cipher(len(data), **kw).encrypt_range(0, data)  # type: ignore[arg-type]


# ------------------------------------------------------ independent vectors


def test_hkdf_matches_rfc_5869_test_case_1() -> None:
    okm = filecrypt.hkdf(
        bytes([0x0B] * 22),
        salt=bytes(range(13)),
        info=bytes(range(0xF0, 0xFA)),
        length=42,
    )
    assert okm.hex() == (
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
    )


def test_a_record_matches_the_gcm_spec_vector() -> None:
    # McGrew & Viega, GCM spec test case 14: AES-256, zero key, zero IV, one zero block.
    # Record 0, not the last, has the all-zero nonce.
    vector = "cea7403d4d606b6e074ec5d3baf39d18d0d1c8a799996bf0265b98b5d48ab919"
    assert filecrypt.encrypt_record(bytes(32), 0, False, bytes(16)).hex() == vector
    # The same block as a file's last record carries the flag, so it differs.
    assert Cipher(bytes(32), 16).encrypt_range(0, bytes(16)).hex() != vector


# ------------------------------------------------------------- round trips


@pytest.mark.parametrize("size", [0, 1, RECORD - 1, RECORD, RECORD + 1, 3 * RECORD, 5 * RECORD + 7])
def test_round_trip(size: int) -> None:
    data = os.urandom(size)
    sealed = seal(data)
    assert len(sealed) == filecrypt.sealed_size(size) == size + TAG * max(1, -(-size // RECORD))
    assert cipher(size).decrypt_range(0, sealed) == data
    assert Decrypting(io.BytesIO(sealed), cipher(size)).read_all() == data


def test_streams_match_whole_file_ciphertext() -> None:
    data = os.urandom(3 * RECORD + 100)
    sealed = seal(data)
    enc = Encrypting(_Trickle(data, 1000), cipher(len(data)))
    assert enc.read_all() == sealed
    assert Decrypting(_Trickle(sealed, 777), cipher(len(data))).read_all() == data


# ------------------------------------------------------------------ attacks


def test_wrong_key_is_refused() -> None:
    sealed = seal(b"secret")
    other = Cipher(filecrypt.file_key(bytes(32), SALT, "d-alice", "d-bob", "d-bob", 0, 6), 6)
    with pytest.raises(DecryptError):
        other.decrypt_range(0, sealed)


@pytest.mark.parametrize("where", [0, 15, RECORD, RECORD + TAG + 3, -1])
def test_any_flipped_bit_is_refused(where: int) -> None:
    data = os.urandom(2 * RECORD + 5)
    sealed = bytearray(seal(data))
    sealed[where] ^= 0x01
    with pytest.raises(DecryptError):
        cipher(len(data)).decrypt_range(0, bytes(sealed))
    with pytest.raises(DecryptError):
        Decrypting(io.BytesIO(bytes(sealed)), cipher(len(data))).read_all()


def test_reordered_records_are_refused() -> None:
    data = os.urandom(3 * RECORD)
    sealed = seal(data)
    n = RECORD + TAG
    swapped = sealed[n : 2 * n] + sealed[:n] + sealed[2 * n :]
    with pytest.raises(DecryptError):
        cipher(len(data)).decrypt_range(0, swapped)


def test_a_record_replayed_at_another_place_is_refused() -> None:
    data = os.urandom(3 * RECORD)
    sealed = seal(data)
    n = RECORD + TAG
    with pytest.raises(DecryptError):
        cipher(len(data)).decrypt_range(RECORD, sealed[:n])  # record 0 offered as record 1


@pytest.mark.parametrize(
    "other",
    [
        {"index": 1},  # another file of the same transfer
        {"target": "w-visitor"},  # the same file for another recipient
        {"salt": os.urandom(32)},  # another transfer
    ],
)
def test_ciphertext_from_another_file_or_transfer_is_refused(other: dict[str, object]) -> None:
    data = os.urandom(RECORD)
    with pytest.raises(DecryptError):
        cipher(len(data)).decrypt_range(0, seal(data, **other))


def test_a_truncated_file_is_refused() -> None:
    data = os.urandom(3 * RECORD)
    sealed = seal(data)
    shorter = Cipher(cipher(len(data)).key, 2 * RECORD)  # receiver told the file is shorter
    with pytest.raises(DecryptError):
        shorter.decrypt_range(0, sealed[: 2 * (RECORD + TAG)])  # last record lacks the flag
    with pytest.raises(DecryptError):
        Decrypting(io.BytesIO(sealed[:-1]), cipher(len(data))).read_all()
    with pytest.raises(DecryptError):
        Decrypting(io.BytesIO(sealed[: RECORD + TAG]), cipher(len(data))).read_all()


def test_extra_bytes_are_refused() -> None:
    data = os.urandom(10)
    with pytest.raises(DecryptError):
        cipher(10).decrypt_range(0, seal(data) + b"x")


def test_nonces_never_repeat_within_a_file() -> None:
    records = 1000
    nonces = {filecrypt.nonce(i, i == records - 1) for i in range(records)}
    assert len(nonces) == records
    assert filecrypt.nonce(5, False) != filecrypt.nonce(5, True)


def test_every_transfer_gets_its_own_keys() -> None:
    salts = {filecrypt.new_salt() for _ in range(100)}
    assert len(salts) == 100
    keys = {cipher(1, salt=bytes.fromhex(s)).key for s in salts}
    assert len(keys) == 100


# ------------------------------------------------------- pieces (resumable)


def test_a_piece_decrypts_on_its_own_and_matches_the_stream() -> None:
    piece = 32 * RECORD  # the 2 MiB pieces apps send
    data = os.urandom(2 * piece + 1234)
    whole = seal(data)
    c = cipher(len(data))
    for start in range(0, len(data), piece):
        part = c.encrypt_range(start, data[start : start + piece])
        at = filecrypt.sealed_size(start) if start else 0
        assert whole[at : at + len(part)] == part
        assert c.decrypt_range(start, part) == data[start : start + piece]


def test_a_misaligned_piece_is_refused() -> None:
    c = cipher(3 * RECORD)
    with pytest.raises(DecryptError):
        c.decrypt_range(100, os.urandom(RECORD + TAG))
    with pytest.raises(ValueError, match="boundary"):
        c.encrypt_range(100, os.urandom(RECORD))


# ------------------------------------------------------------- memory


def test_a_large_file_streams_in_constant_memory() -> None:
    size = 64 * 1024 * 1024
    tracemalloc.start()
    try:
        enc = Encrypting(_Zeros(size), cipher(size))
        dec = Decrypting(enc, cipher(size))
        seen = 0
        while chunk := dec.read(256 * 1024):
            seen += len(chunk)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert seen == size
    assert peak < 2 * 1024 * 1024, peak


# --------------------------------------------------------- metadata, verify


def test_metadata_is_sealed_and_bound_to_its_transfer() -> None:
    key = filecrypt.meta_key(KEY, SALT, "d-alice", "d-bob", "d-bob")
    meta = {"files": [{"name": "tax-return.pdf", "mime": "application/pdf"}]}
    sealed = filecrypt.seal_meta(key, meta)
    assert "tax-return" not in sealed
    assert filecrypt.open_meta(key, sealed) == meta
    other = filecrypt.meta_key(KEY, os.urandom(32), "d-alice", "d-bob", "d-bob")
    with pytest.raises(DecryptError):
        filecrypt.open_meta(other, sealed)
    with pytest.raises(DecryptError):
        filecrypt.open_meta(key, sealed[:-2] + "AA")


def test_the_checksum_confirmation_reveals_no_hash() -> None:
    digest = "ab" * 32
    mac = filecrypt.verify_mac(cipher(1).key, digest)
    assert mac != digest
    assert mac == filecrypt.verify_mac(cipher(1).key, digest)
    assert mac != filecrypt.verify_mac(cipher(1, index=1).key, digest)


# ----------------------------------------------------------------- helpers


class _Trickle:
    """A stream that hands out at most ``step`` bytes per read, like a socket."""

    def __init__(self, data: bytes, step: int) -> None:
        self._io, self._step = io.BytesIO(data), step

    def read(self, n: int = -1) -> bytes:
        return self._io.read(min(self._step, n) if n >= 0 else self._step)


class _Zeros:
    def __init__(self, size: int) -> None:
        self._left = size

    def read(self, n: int = -1) -> bytes:
        n = self._left if n < 0 else min(n, self._left)
        self._left -= n
        return bytes(n)
