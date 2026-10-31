"""SRP-6a: correctness, interop with an independent implementation, refusals."""

from __future__ import annotations

import random

import pytest

from open_transfer import srp


def _is_probable_prime(n: int, rounds: int = 16) -> bool:
    if n < 2 or n % 2 == 0:
        return n == 2
    d, s = n - 1, 0
    while d % 2 == 0:
        d, s = d // 2, s + 1
    rng = random.Random(1)
    for _ in range(rounds):
        x = pow(rng.randrange(2, n - 1), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def test_group_is_a_2048_bit_safe_prime() -> None:
    assert srp.N.bit_length() == 2048
    assert _is_probable_prime(srp.N)
    assert _is_probable_prime((srp.N - 1) // 2)


def test_same_code_agrees_on_a_key_and_both_sides_prove_it() -> None:
    server = srp.Server("d-a|d-b", "123456")
    client = srp.Client("d-a|d-b", "123456")
    proof = client.respond(server.salt, server.b_pub)
    client.check(server.verify(client.a_pub, proof))
    assert client.key == server.key
    assert len(client.key or b"") == 32


def test_wrong_code_is_refused_on_both_sides() -> None:
    server = srp.Server("d-a|d-b", "123456")
    client = srp.Client("d-a|d-b", "654321")
    proof = client.respond(server.salt, server.b_pub)
    with pytest.raises(srp.SRPError):
        server.verify(client.a_pub, proof)
    with pytest.raises(srp.SRPError):
        client.check(b"\0" * 32)  # an impostor server can't produce a valid proof


def test_ids_are_bound_into_the_key() -> None:
    server = srp.Server("d-a|d-b", "123456")
    client = srp.Client("d-a|d-x", "123456")  # same code, someone else's ids
    with pytest.raises(srp.SRPError):
        server.verify(client.a_pub, client.respond(server.salt, server.b_pub))


@pytest.mark.parametrize("bad", [0, srp.N, 2 * srp.N])
def test_degenerate_public_values_are_refused(bad: int) -> None:
    with pytest.raises(srp.SRPError):
        srp.Server("i", "123456").verify(bad, b"\0" * 32)
    with pytest.raises(srp.SRPError):
        srp.Client("i", "123456").respond(b"salt", bad)


def test_fresh_values_every_attempt() -> None:
    a, b = srp.Server("i", "123456"), srp.Server("i", "123456")
    assert a.salt != b.salt
    assert a.b_pub != b.b_pub
    assert srp.Client("i", "1").a_pub != srp.Client("i", "1").a_pub


def test_interoperates_with_pysrp() -> None:
    """An independent RFC 5054 implementation agrees with ours, both ways."""
    pysrp = pytest.importorskip("srp._pysrp")
    pysrp.rfc5054_enable()
    try:
        ident, code = "d-a|d-b", "246810"
        # their client, our server
        ours = srp.Server(ident, code)
        theirs = pysrp.User(ident, code, hash_alg=pysrp.SHA256, ng_type=pysrp.NG_2048)
        _, a_bytes = theirs.start_authentication()
        m1 = theirs.process_challenge(ours.salt, srp.to_bytes(ours.b_pub))
        theirs.verify_session(ours.verify(int.from_bytes(a_bytes, "big"), m1))
        assert theirs.authenticated()
        assert theirs.get_session_key() == ours.key

        # our client, their server
        salt, v = pysrp.create_salted_verification_key(
            ident, code, hash_alg=pysrp.SHA256, ng_type=pysrp.NG_2048
        )
        client = srp.Client(ident, code)
        server = pysrp.Verifier(
            ident, salt, v, srp.to_bytes(client.a_pub), hash_alg=pysrp.SHA256,
            ng_type=pysrp.NG_2048,
        )  # fmt: skip
        s, b_bytes = server.get_challenge()
        m2 = server.verify_session(client.respond(s, int.from_bytes(b_bytes, "big")))
        assert m2 is not None
        client.check(m2)
        assert server.get_session_key() == client.key
    finally:
        pysrp.rfc5054_enable(False)
