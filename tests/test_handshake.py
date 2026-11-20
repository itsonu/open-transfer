"""The per-transfer key agreement (docs/encryption.md, "Forward secrecy").

Established primitives only, checked here against their published test vectors,
then the handshake built from them: what it binds, what it refuses.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from open_transfer import filecrypt
from open_transfer.filecrypt import Handshake

h = bytes.fromhex

# ------------------------------------------------------- published vectors


def test_x25519_rfc7748_section_6_1() -> None:
    alice = X25519PrivateKey.from_private_bytes(
        h("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    )
    bob = X25519PrivateKey.from_private_bytes(
        h("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
    )
    assert alice.public_key().public_bytes_raw() == h(
        "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    )
    assert bob.public_key().public_bytes_raw() == h(
        "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f"
    )
    shared = h("4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742")
    assert alice.exchange(bob.public_key()) == shared == bob.exchange(alice.public_key())


def test_hkdf_sha256_rfc5869_test_case_1() -> None:
    okm = HKDF(hashes.SHA256(), 42, bytes(range(13)), bytes(range(0xF0, 0xFA))).derive(b"\x0b" * 22)
    assert okm == h(
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
    )
    # filecrypt.hkdf is that same HKDF.
    assert filecrypt.hkdf(b"\x0b" * 22, salt=bytes(range(13)), info=bytes(range(0xF0, 0xFA)), length=42) == okm  # fmt: skip


def test_hmac_sha256_rfc4231_test_case_2() -> None:
    mac = hmac.new(b"Jefe", b"what do ya want for nothing?", hashlib.sha256).hexdigest()
    assert mac == "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"


def test_aes_256_gcm_mcgrew_viega_test_cases_13_and_14() -> None:
    gcm = AESGCM(bytes(32))
    assert gcm.encrypt(bytes(12), b"", None) == h("530f8afbc74536b9a963b4f1c4cb738b")
    assert gcm.encrypt(bytes(12), bytes(16), None) == h(
        "cea7403d4d606b6e074ec5d3baf39d18d0d1c8a799996bf0265b98b5d48ab919"
    )


# --------------------------------------------------------------- handshake

SALT = b"s" * 32
PAIR = b"p" * 32
IDS = {"sender": "d-a", "receiver": "d-b", "job": "J1", "target": "d-b"}


def run(
    pair_key: bytes | None = PAIR, receiver_pair: bytes | None = PAIR, **change: str
) -> tuple[Handshake, Handshake, bytes, bytes]:
    """A full handshake; ``change`` alters what the receiver thinks it's for."""
    run_key, kx = filecrypt.new_kx()  # the receiver's per-run key
    sender = Handshake(pair_key, SALT, **IDS)
    eph = sender.offer(kx)
    receiver = Handshake(receiver_pair, SALT, **{**IDS, **change})
    eph_r, confirm = receiver.answer(run_key, eph)
    return sender, receiver, eph_r, confirm


def test_both_ends_agree_on_the_session_key() -> None:
    sender, receiver, eph_r, confirm = run()
    assert sender.meta_key == receiver.meta_key
    session = sender.finish(eph_r, confirm)
    assert session == receiver.session
    assert len(session) == 32


def test_every_handshake_has_new_ephemeral_keys_and_a_new_session() -> None:
    first, _, eph_r1, c1 = run()
    second, _, eph_r2, c2 = run()
    assert first.eph != second.eph
    assert eph_r1 != eph_r2
    assert first.finish(eph_r1, c1) != second.finish(eph_r2, c2)


@pytest.mark.parametrize(
    "change",
    [{"sender": "d-x"}, {"receiver": "d-x"}, {"job": "J2"}, {"target": "v-other"}],
)
def test_the_handshake_is_bound_to_who_and_what(change: dict[str, str]) -> None:
    sender, _, eph_r, confirm = run(**change)
    with pytest.raises(filecrypt.DecryptError):
        sender.finish(eph_r, confirm)


def test_a_wrong_pair_key_fails() -> None:
    sender, _, eph_r, confirm = run(receiver_pair=b"q" * 32)
    with pytest.raises(filecrypt.DecryptError):
        sender.finish(eph_r, confirm)


def test_unpaired_needs_no_pair_key_but_a_paired_offer_cant_be_read_as_unpaired() -> None:
    sender, receiver, eph_r, confirm = run(pair_key=None, receiver_pair=None)
    assert sender.finish(eph_r, confirm) == receiver.session
    sender, _, eph_r, confirm = run(pair_key=PAIR, receiver_pair=None)
    with pytest.raises(filecrypt.DecryptError):
        sender.finish(eph_r, confirm)


def test_a_changed_answer_fails() -> None:
    sender, _, eph_r, confirm = run()
    other = filecrypt.new_kx()[1]
    with pytest.raises(filecrypt.DecryptError):
        sender.finish(other, confirm)  # someone else's key
    with pytest.raises(filecrypt.DecryptError):
        sender.finish(eph_r, bytes(32))  # a forged confirmation


def test_an_answer_from_another_handshake_fails() -> None:
    _, _, eph_r1, c1 = run()
    second, _, _, _ = run()
    with pytest.raises(filecrypt.DecryptError):
        second.finish(eph_r1, c1)  # replayed or moved from another transfer


def test_the_version_is_in_the_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    run_key, kx = filecrypt.new_kx()
    sender = Handshake(PAIR, SALT, **IDS)
    eph = sender.offer(kx)
    monkeypatch.setattr(filecrypt, "VERSION", 99)
    eph_r, confirm = Handshake(PAIR, SALT, **IDS).answer(run_key, eph)
    with pytest.raises(filecrypt.DecryptError):
        sender.finish(eph_r, confirm)


def test_a_degenerate_key_is_refused() -> None:
    run_key, _ = filecrypt.new_kx()
    with pytest.raises(filecrypt.DecryptError):
        Handshake(PAIR, SALT, **IDS).answer(run_key, bytes(32))  # all-zero shared secret
    with pytest.raises(filecrypt.DecryptError):
        Handshake(PAIR, SALT, **IDS).offer(bytes(32))


def test_ephemeral_private_keys_are_dropped() -> None:
    sender, receiver, eph_r, confirm = run()
    assert receiver._mine is None  # dropped as soon as the session key exists
    sender.finish(eph_r, confirm)
    assert sender._mine is None


def test_file_signal_and_meta_keys_are_independent() -> None:
    sender, _, eph_r, confirm = run()
    session = sender.finish(eph_r, confirm)
    file_key = filecrypt.file_key(session, SALT, "d-a", "d-b", "d-b", 0, 10)
    there = filecrypt.signal_key(session, SALT, "d-a", "d-b", "d-b", "to_receiver")
    back = filecrypt.signal_key(session, SALT, "d-a", "d-b", "d-b", "to_sender")
    assert len({file_key, there, back, sender.meta_key, session}) == 5
