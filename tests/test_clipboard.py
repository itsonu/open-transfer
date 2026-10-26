"""Clipboard: spotting sensitive text, the history kept on each app, and syncing
between paired devices that both allowed it."""

from __future__ import annotations

import base64
import os
import stat
import time
from pathlib import Path

import pytest

from open_transfer.clipboard import ClipboardStore
from open_transfer.mesh import P2P
from open_transfer.sensitive import looks_sensitive
from tests.conftest import NodeFactory
from tests.test_mesh import Client, device_id, owner, wait_for

# ------------------------------------------------------------ sensitive text


SENSITIVE_CASES = [
    ("4111 1111 1111 1111", "card"),
    ("4111-1111-1111-1111", "card"),
    ("482913", "code"),
    ("Your code is 12 34 56", None),  # a sentence, not a bare code
    (
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----",
        "key",
    ),
    ("ghp_aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5", "token"),
    ("sk-proj-abcdefghijklmnopqrstuvwxyz0123456789", "token"),
    ("AKIAIOSFODNN7EXAMPLE", "token"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlc2lnbmF0dXJl", "token"),
    ("password: hunter22", "password"),
    ("Tr0ub4dor&3xQ!", "password"),
    ("hello world", None),
    ("https://example.com/some/page", None),
    ("Meeting at 10:30 in room 4", None),
    ("4111 1111 1111 1112", None),  # fails the Luhn check
    ("2024", None),  # a year, not a code
    ("", None),
]


@pytest.mark.parametrize(("text", "reason"), SENSITIVE_CASES)
def test_looks_sensitive(text: str, reason: str | None) -> None:
    assert looks_sensitive(text) == reason


# --------------------------------------------------------------------- store


def test_history_keeps_newest_first_and_respects_retention(tmp_path: Path) -> None:
    store = ClipboardStore(tmp_path)
    store.set_settings(max_items=3)
    for i in range(5):
        store.add(
            kind="text", text=f"item {i}", direction="sent", device={"id": "d-x", "name": "X"}
        )
    assert [i["text"] for i in store.items()] == ["item 4", "item 3", "item 2"]
    # Survives a restart.
    again = ClipboardStore(tmp_path)
    assert [i["text"] for i in again.items()] == ["item 4", "item 3", "item 2"]
    assert again.settings()["max_items"] == 3


def test_old_items_expire(tmp_path: Path) -> None:
    store = ClipboardStore(tmp_path)
    store.set_settings(max_age_days=1)
    old = store.add(kind="text", text="old", direction="sent", device={"id": "d-x", "name": "X"})
    store._items[0]["at"] = time.time() - 2 * 86400
    store.add(kind="text", text="new", direction="sent", device={"id": "d-x", "name": "X"})
    assert [i["text"] for i in store.items()] == ["new"]
    assert store.get(old["id"]) is None


def test_search_delete_and_clear(tmp_path: Path) -> None:
    store = ClipboardStore(tmp_path)
    a = store.add(
        kind="text",
        text="Wi-Fi password is on the fridge",
        direction="received",
        device={"id": "d-b", "name": "Bravo"},
    )
    store.add(
        kind="text",
        text="https://example.com",
        direction="sent",
        device={"id": "d-b", "name": "Bravo"},
    )
    assert [i["id"] for i in store.items(query="fridge")] == [a["id"]]
    assert len(store.items(query="bravo")) == 2  # the device name matches too
    deleted, again = store.delete(a["id"]), store.delete(a["id"])
    assert deleted
    assert not again
    store.clear()
    assert store.items() == []


def test_sensitive_items_are_never_kept(tmp_path: Path) -> None:
    store = ClipboardStore(tmp_path)
    item = store.add(
        kind="text",
        text="4111 1111 1111 1111",
        direction="received",
        device={"id": "d-b", "name": "Bravo"},
    )
    assert item["transient"] is True
    assert store.items()[0]["id"] == item["id"]  # there to copy for a moment…
    saved = tmp_path / "clipboard.json"
    assert not saved.exists() or "4111" not in saved.read_text()  # …but never written down
    assert ClipboardStore(tmp_path).items() == []


def test_images_are_stored_as_files_and_removed_with_their_item(tmp_path: Path) -> None:
    store = ClipboardStore(tmp_path)
    store.set_settings(max_items=1)
    first = store.add(
        kind="image",
        data=b"\x89PNG one",
        mime="image/png",
        direction="sent",
        device={"id": "d-x", "name": "X"},
    )
    path = store.image_path(first["id"])
    assert path is not None
    assert path.read_bytes() == b"\x89PNG one"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600  # private, like clipboard.json
    store.add(
        kind="image",
        data=b"\x89PNG two",
        mime="image/png",
        direction="sent",
        device={"id": "d-x", "name": "X"},
    )
    assert not path.exists()  # pruned with its item


def test_settings_are_validated(tmp_path: Path) -> None:
    store = ClipboardStore(tmp_path)
    with pytest.raises(ValueError, match="items"):
        store.set_settings(max_items=0)
    with pytest.raises(ValueError, match="days"):
        store.set_settings(max_age_days=-1)
    device = "d-" + "ab" * 12
    store.set_settings(allowed=[device], max_items=200, max_age_days=0)  # 0 = keep
    assert store.settings() == {"allowed": [device], "max_items": 200, "max_age_days": 0}
    with pytest.raises(ValueError, match="device"):
        store.set_settings(allowed=["../etc"])


# ---------------------------------------------------- syncing between apps


def pair(a_client: Client, b_client: Client, b_port: int) -> None:
    code = b_client.state()["pairing"]["code"]
    status, data = a_client("POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{b_port}"})
    assert status == 200, data


def allow(client: Client, *ids: str) -> None:
    status, data = client("PUT", "/api/clipboard/settings", {"allowed": list(ids)})
    assert status == 200, data


def push_text(client: Client, text: str, **extra: object) -> tuple[int, dict]:  # type: ignore[type-arg]
    return client("POST", "/api/clipboard", {"kind": "text", "text": text, **extra})  # type: ignore[no-any-return]


@pytest.fixture
def paired(make_node: NodeFactory) -> tuple[Client, Client, str, str]:
    a, b = make_node("Alpha"), make_node("Bravo")
    alice, bob = owner(a), owner(b)
    pair(alice, bob, b.port)
    bravo, alpha = device_id(alice, "Bravo"), device_id(bob, "Alpha")
    return alice, bob, alpha, bravo


def test_text_syncs_to_a_paired_device_that_allowed_it(
    paired: tuple[Client, Client, str, str],
) -> None:
    alice, bob, alpha, bravo = paired
    allow(alice, bravo)
    allow(bob, alpha)
    status, data = push_text(alice, "See you at 7")
    assert status == 200, data
    assert [d["name"] for d in data["delivered"]] == ["Bravo"]
    received = wait_for(lambda: bob("GET", "/api/clipboard")[1]["items"])
    assert received[0]["text"] == "See you at 7"
    assert received[0]["direction"] == "received"
    assert received[0]["device"]["name"] == "Alpha"
    assert bob.state()["clipboard"]["latest"] == received[0]["id"]
    sent = alice("GET", "/api/clipboard")[1]["items"]
    assert sent[0]["direction"] == "sent"


def test_nothing_is_sent_unless_both_sides_allow_it(
    paired: tuple[Client, Client, str, str],
) -> None:
    alice, bob, _, bravo = paired
    status, data = push_text(alice, "nobody allowed")
    assert status == 200, data
    assert data["delivered"] == []  # Alpha didn't allow anyone
    allow(alice, bravo)
    status, data = push_text(alice, "only one side")
    assert data["delivered"] == []
    assert [f["name"] for f in data["failed"]] == ["Bravo"]
    assert bob("GET", "/api/clipboard")[1]["items"] == []


def test_only_paired_devices_can_be_allowed(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    alice = owner(a)
    status, _ = alice("POST", "/api/devices", {"address": f"127.0.0.1:{b.port}"})
    assert status == 200
    status, data = alice("PUT", "/api/clipboard/settings", {"allowed": [device_id(alice, "Bravo")]})
    assert status == 400
    assert data["error"]["code"] == "not_paired"


def test_unsigned_clips_are_refused(paired: tuple[Client, Client, str, str]) -> None:
    _, bob, alpha, _ = paired
    allow(bob, alpha)
    # Pretend to be Alpha without its key.
    status, data = Client(bob.base)(
        "POST",
        f"{P2P}/clip",
        {"from": {"id": alpha, "name": "Alpha"}, "kind": "text", "text": "spoofed"},
        headers={"X-OT-From": alpha},
    )
    assert status == 403, data
    assert bob("GET", "/api/clipboard")[1]["items"] == []


def test_sensitive_text_needs_confirming_and_is_never_kept(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    alice, bob = owner(a), owner(b)
    pair(alice, bob, b.port)
    allow(alice, device_id(alice, "Bravo"))
    allow(bob, device_id(bob, "Alpha"))
    status, data = push_text(alice, "4111 1111 1111 1111")
    assert status == 409
    assert data["error"]["code"] == "sensitive"
    assert data["error"]["reason"] == "card"
    status, data = push_text(alice, "4111 1111 1111 1111", confirm_sensitive=True)
    assert status == 200, data
    assert [d["name"] for d in data["delivered"]] == ["Bravo"]
    item = wait_for(lambda: bob("GET", "/api/clipboard")[1]["items"])[0]
    assert item["transient"] is True  # there to copy for a moment, never written down
    for node in (a, b):
        for path in node.config.state_path.rglob("*"):
            if path.is_file():
                # The whole number: keys, ids and sequence numbers can contain "4111".
                assert b"4111 1111 1111 1111" not in path.read_bytes(), path


def test_images_sync_and_are_capped(paired: tuple[Client, Client, str, str]) -> None:
    alice, bob, alpha, bravo = paired
    allow(alice, bravo)
    allow(bob, alpha)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    status, data = alice(
        "POST",
        "/api/clipboard",
        {"kind": "image", "mime": "image/png", "data": base64.b64encode(png).decode()},
    )
    assert status == 200, data
    item = wait_for(lambda: bob("GET", "/api/clipboard")[1]["items"])[0]
    assert item["kind"] == "image"
    status, raw = bob("GET", f"/api/clipboard/{item['id']}/image", raw=True)
    assert status == 200
    assert raw == png
    big = base64.b64encode(b"\x00" * (10 * 1024 * 1024 + 1)).decode()
    status, data = alice(
        "POST", "/api/clipboard", {"kind": "image", "mime": "image/png", "data": big}
    )
    assert status == 413
    status, data = push_text(alice, "x" * (1024 * 1024 + 1))
    assert status == 413


def test_visitors_have_no_clipboard_sync(make_node: NodeFactory) -> None:
    a = make_node("Alpha", owner_loopback=False)
    visitor = Client(a.local_url)
    assert visitor("GET", "/api/clipboard")[0] == 403
    assert push_text(visitor, "hi")[0] == 403
