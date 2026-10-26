"""One device, one tile. A phone that is reinstalled (Android wipes its data when an
APK signed with a different key replaces it) comes back with a new id; pairing again
must replace the old pairing, not add a second "Nirakara" — and old twins already
saved show once."""

from __future__ import annotations

from pathlib import Path

from open_transfer.node import Node
from tests.conftest import NodeFactory
from tests.test_mesh import owner, wait_for


def pair(a: Node, b: Node) -> None:
    code = owner(b).state()["pairing"]["code"]
    status, data = owner(a)("POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{b.port}"})
    assert status == 200, data


def gone(viewer: Node, node: Node) -> None:
    """``node`` stopped and ``viewer`` no longer hears it."""
    node.stop()
    peer = viewer.mesh._peers.get(node.mesh.id)
    if peer is not None:
        peer.last_seen = 0.0


def tiles(node: Node, name: str) -> list[dict]:  # type: ignore[type-arg]
    return [d for d in owner(node).state()["devices"] if d["name"] == name]


def test_a_reinstalled_phone_replaces_its_old_pairing(
    make_node: NodeFactory, tmp_path: Path
) -> None:
    mac = make_node("Mac")
    phone = make_node("Nirakara", device_platform="android", device_form="phone")
    pair(mac, phone)
    old_id = phone.mesh.id
    gone(mac, phone)

    again = make_node(
        "Nirakara", device_platform="android", device_form="phone", state_dir=tmp_path / "new"
    )
    assert again.mesh.id != old_id  # wiped by the reinstall
    pair(mac, again)

    assert mac.mesh.trust.get(old_id) is None
    assert [d["id"] for d in tiles(mac, "Nirakara")] == [again.mesh.id]


def test_the_device_showing_the_code_forgets_the_old_one_too(
    make_node: NodeFactory, tmp_path: Path
) -> None:
    phone = make_node("Pixel", device_platform="android", device_form="phone")
    mac = make_node("Nirakara-Mac", device_platform="macos")
    pair(phone, mac)
    old_id = mac.mesh.id
    gone(phone, mac)
    again = make_node("Nirakara-Mac", device_platform="macos", state_dir=tmp_path / "new")
    pair(again, phone)  # this time the phone shows the code
    assert phone.mesh.trust.get(old_id) is None
    assert phone.mesh.trust.get(again.mesh.id) is not None


def test_two_real_devices_with_one_name_are_both_kept(
    make_node: NodeFactory, tmp_path: Path
) -> None:
    mac = make_node("Mac")
    first = make_node("Laptop", device_platform="windows")
    second = make_node("Laptop", device_platform="windows", state_dir=tmp_path / "second")
    pair(mac, first)
    pair(mac, second)  # the first is still around: it's another device
    assert mac.mesh.trust.get(first.mesh.id) is not None
    assert mac.mesh.trust.get(second.mesh.id) is not None


def test_a_different_kind_of_device_with_the_name_is_kept(
    make_node: NodeFactory, tmp_path: Path
) -> None:
    mac = make_node("Mac")
    pc = make_node("Sonu", device_platform="windows")
    pair(mac, pc)
    gone(mac, pc)
    phone = make_node("Sonu", device_platform="android", state_dir=tmp_path / "phone")
    pair(mac, phone)
    assert mac.mesh.trust.get(pc.mesh.id) is not None


def test_old_twins_already_saved_show_once(make_node: NodeFactory) -> None:
    mac = make_node("Mac")
    for n, stamp in enumerate((100.0, 300.0, 200.0)):
        mac.mesh.trust.add("d-" + f"{n:02d}" * 12, "Nirakara", b"k" * 32, at=stamp)
    shown = tiles(mac, "Nirakara")
    assert len(shown) == 1
    assert shown[0]["id"] == "d-" + "01" * 12  # the newest pairing
    assert shown[0]["paired"] is True


def test_an_away_device_keeps_its_icon(make_node: NodeFactory) -> None:
    mac = make_node("Mac")
    phone = make_node("Nirakara", device_platform="android", device_form="phone")
    pair(mac, phone)
    gone(mac, phone)
    wait_for(lambda: not tiles(mac, "Nirakara")[0]["online"])
    [tile] = tiles(mac, "Nirakara")
    assert (tile["form"], tile["platform"]) == ("phone", "android")


def test_old_pairing_files_still_load(tmp_path: Path) -> None:
    import json

    from open_transfer.devices import TrustStore

    (tmp_path / "trusted.json").write_text(
        json.dumps({"d-" + "ab" * 12: {"name": "Old", "key": "aa" * 32, "paired_at": 1.0}})
    )
    [device] = TrustStore(tmp_path).all()
    assert (device.name, device.form, device.platform) == ("Old", "", "")
