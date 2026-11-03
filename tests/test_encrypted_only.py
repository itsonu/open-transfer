"""“Encrypted transfers only”: no plain app-to-app transfer, either way (docs/encryption.md).

Off by default so older versions keep working (their transfers are labelled
"not encrypted"). On, this app refuses to send to or take files from an app that
can't encrypt, and says what to do about it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from open_transfer.config import Config
from open_transfer.node import Node
from tests.conftest import NodeFactory
from tests.test_encrypted_transfers import paired, target
from tests.test_mesh import (
    connect,
    device_id,
    incoming,
    owner,
    send,
    target_states,
    upload,
    visitor,
    wait_for,
)
from tests.test_moves import pair


def older(node: Node, monkeypatch: pytest.MonkeyPatch, *keys: str) -> None:
    """``node`` is from before ot-file/1: it doesn't advertise ``keys``."""
    real = node.mesh.self_info
    monkeypatch.setattr(
        node.mesh, "self_info", lambda **kw: {k: v for k, v in real(**kw).items() if k not in keys}
    )


def plain_sender(node: Node, monkeypatch: pytest.MonkeyPatch) -> None:
    """``node`` sends like an older app: its offers have no "enc" block."""
    real = node.mesh._http

    def old(peer: Any, method: str, path: str, **kw: Any) -> tuple[int, dict[str, Any]]:
        if kw.get("body"):
            kw["body"] = {k: v for k, v in kw["body"].items() if k != "enc"}
        return real(peer, method, path, **kw)

    monkeypatch.setattr(node.mesh, "_http", old)


def test_sending_to_an_older_app_is_refused_with_a_reason(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = make_node("Alpha", encrypted_only=True), make_node("Bravo", encrypted_only=False)
    older(b, monkeypatch, "kx")
    connect(a, b)
    alice = owner(a)
    job = send(alice, [device_id(alice, "Bravo")], [("plans.txt", b"plans")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "failed"})
    reason = target(a, job)["reason"]
    assert "encrypt" in reason
    assert "Update" in reason  # what to do about it
    assert owner(b).state()["incoming"] == []  # nobody was even asked


def test_an_older_paired_app_that_ignores_encryption_gets_nothing(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = make_node("Alpha", encrypted_only=True), make_node("Bravo", encrypted_only=False)
    older(b, monkeypatch, "enc")
    plain_sender(a, monkeypatch)  # what an older Bravo answers looks the same: no "enc"
    pair(a, b)
    alice = owner(a)
    job = send(alice, [b.mesh.id], [("plans.txt", b"plans")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "failed"})
    assert "encrypt" in target(a, job)["reason"]
    assert not (Path(b.config.storage_dir) / "plans.txt").exists()


def test_taking_files_from_an_older_app_is_refused_with_a_reason(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = make_node("Alpha"), make_node("Bravo", encrypted_only=True)
    plain_sender(a, monkeypatch)
    connect(a, b)
    alice = owner(a)
    job = send(alice, [device_id(alice, "Bravo")], [("plans.txt", b"plans")])
    wait_for(lambda: target_states(alice, job) in ({"Bravo": "declined"}, {"Bravo": "failed"}))
    reason = target(a, job)["reason"]
    assert "only encrypted" in reason
    assert "Update" in reason
    assert owner(b).state()["incoming"] == []


@pytest.mark.parametrize("mode", ["paired", "unpaired"])
def test_encrypted_transfers_still_go_through(make_node: NodeFactory, mode: str) -> None:
    if mode == "paired":
        a, b = paired(make_node)
        a.mesh.config.encrypted_only = b.mesh.config.encrypted_only = True
        to = b.mesh.id
    else:
        a = make_node("Alpha", encrypted_only=True)
        b = make_node("Bravo", encrypted_only=True, auto_accept=True)
        connect(a, b)
        to = device_id(owner(a), "Bravo")
    alice = owner(a)
    job = send(alice, [to], [("ok.txt", b"ok")])
    wait_for(lambda: target_states(alice, job) == {"Bravo": "accepted"})
    assert upload(alice, job, 0, b"ok")[0] == 200
    assert (Path(b.config.storage_dir) / "ok.txt").read_bytes() == b"ok"
    assert target(a, job)["encrypted"] is True


def test_browsers_on_this_app_still_work(make_node: NodeFactory) -> None:
    # Its own visitors don't cross between apps (their hop is HTTP, or TLS with --https).
    a = make_node("Alpha", encrypted_only=True)
    phone, tablet = visitor(a, "Phone"), visitor(a, "Tablet")
    job = send(phone, [device_id(phone, "Tablet")], [("x.txt", b"x")])
    tablet("POST", f"/api/incoming/{incoming(tablet)['id']}/accept")
    wait_for(lambda: target_states(phone, job) == {"Tablet": "accepted"})
    assert upload(phone, job, 0, b"x")[0] == 200


def test_the_owner_turns_it_on_and_it_stays_on(tmp_path: Path) -> None:
    def start() -> Node:
        node = Node(Config(storage_dir=tmp_path / "share", port=0, discovery=False))
        node.start()
        return node

    node = start()
    try:
        assert owner(node).state()["settings"]["encrypted_only"] is True  # a new install
        status, data = owner(node)("PUT", "/api/settings", {"encrypted_only": False})
        assert status == 200, data
        assert data["settings"]["encrypted_only"] is False
        status, data = owner(node)("PUT", "/api/settings", {"encrypted_only": True})
        assert data["settings"]["encrypted_only"] is True
        assert visitor(node, "Phone")("PUT", "/api/settings", {"encrypted_only": False})[0] == 403
        assert owner(node)("PUT", "/api/settings", {"encrypted_only": "yes"})[0] == 400
    finally:
        node.stop()
    again = start()
    try:
        assert owner(again).state()["settings"]["encrypted_only"] is True
    finally:
        again.stop()


def test_a_new_installation_starts_encrypted_only(tmp_path: Path) -> None:
    node = Node(Config(storage_dir=tmp_path / "share", port=0, discovery=False))
    node.start()
    try:
        assert owner(node).state()["settings"]["encrypted_only"] is True
    finally:
        node.stop()


def test_an_existing_installation_keeps_working_with_older_apps(tmp_path: Path) -> None:
    from open_transfer.devices import IdentityStore

    state = tmp_path / "share" / ".open-transfer"
    state.mkdir(parents=True)
    IdentityStore(state, name="Old", form="computer", platform="macos")  # from before this version
    node = Node(Config(storage_dir=tmp_path / "share", port=0, discovery=False))
    node.start()
    try:
        assert owner(node).state()["settings"]["encrypted_only"] is False
    finally:
        node.stop()
    # Recorded once: the owner's choice from now on, and the switch turns it on.
    assert (state / "settings.json").exists()
    again = Node(Config(storage_dir=tmp_path / "share", port=0, discovery=False))
    again.start()
    try:
        assert owner(again).state()["settings"]["encrypted_only"] is False
        owner(again)("PUT", "/api/settings", {"encrypted_only": True})
    finally:
        again.stop()
