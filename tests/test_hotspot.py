"""Phone hotspots and network switches (H7).

A device that *is* the hotspot (an Android phone, a Mac sharing its
connection, a Windows Mobile Hotspot) has its hotspot address on an interface
that is neither the route to the internet nor in its host name's addresses.
Hotspots also often drop multicast, so discovery can't be the only way in.
"""

from __future__ import annotations

import errno
import sys

import pytest

from open_transfer import network
from open_transfer.mesh import MeshError, Peer
from open_transfer.network import neighbours  # the real one (conftest stubs it)
from tests.conftest import NodeFactory
from tests.test_mesh import lan_ip, owner, wait_for
from tests.test_moves import pair


def fake_net(
    monkeypatch: pytest.MonkeyPatch, ifaces: list[tuple[str, str]], primary: str | None
) -> None:
    monkeypatch.setattr(network, "interface_ips", lambda: ifaces)
    monkeypatch.setattr(network, "primary_ip", lambda: primary)
    monkeypatch.setattr(network, "_host_ips", lambda wait=1.0: [])


# ---------------------------------------------------------- which address


def test_an_android_phone_sharing_its_hotspot_uses_the_hotspot_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mobile data is the route out; nobody nearby can reach that address.
    fake_net(
        monkeypatch,
        [("lo", "127.0.0.1"), ("rmnet_data0", "10.71.3.9"), ("ap0", "192.168.43.1")],
        primary="10.71.3.9",
    )
    assert network.lan_ips() == ["192.168.43.1"]


def test_an_android_phone_never_offers_its_mobile_data_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On IPv6-only mobile data Android routes IPv4 through 464XLAT, whose address is
    # 192.0.0.4. Android apps get no interface list, so only the address says so.
    fake_net(monkeypatch, [], primary="192.0.0.4")
    assert network.lan_ips() == []


def test_a_phone_hotspot_with_no_mobile_data_still_has_an_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_net(monkeypatch, [("lo", "127.0.0.1"), ("swlan0", "10.42.17.1")], primary=None)
    assert network.lan_ips() == ["10.42.17.1"]


def test_a_mac_on_a_phone_hotspot_with_mobile_data_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase L: the Mac is a client of the phone's hotspot, and nothing routes to the internet."""
    fake_net(
        monkeypatch,
        [("lo0", "127.0.0.1"), ("en0", "172.20.10.4"), ("awdl0", "169.254.7.7")],
        primary=None,  # no default route: the "route out" lookup finds nothing
    )
    assert network.lan_ips() == ["172.20.10.4"]
    assert "172.20.10.1" in neighbours(network.lan_ips())  # the phone, to sweep for


def test_a_mac_sharing_its_connection_lists_both_networks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_net(
        monkeypatch,
        [
            ("lo0", "127.0.0.1"),
            ("en0", "192.168.1.20"),
            ("bridge100", "192.168.2.1"),  # Internet Sharing's hotspot
            ("utun4", "100.101.2.3"),  # a VPN
            ("awdl0", "169.254.3.3"),
        ],
        primary="192.168.1.20",
    )
    assert network.lan_ips() == ["192.168.1.20", "192.168.2.1"]


def test_vpn_and_container_interfaces_are_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_net(
        monkeypatch,
        [
            ("eth0", "192.168.0.5"),
            ("docker0", "172.17.0.1"),
            ("veth12ab", "172.18.0.1"),
            ("tun0", "10.8.0.2"),
            ("wg0", "10.9.0.2"),
            ("tailscale0", "100.64.0.7"),
        ],
        primary="192.168.0.5",
    )
    assert network.lan_ips() == ["192.168.0.5"]


def test_a_windows_mobile_hotspot_address_comes_from_the_host_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Windows has no interface list here; its host name resolves to every adapter.
    monkeypatch.setattr(network, "interface_ips", list)
    monkeypatch.setattr(network, "primary_ip", lambda: "192.168.1.30")
    monkeypatch.setattr(network, "_host_ips", lambda wait=1.0: ["192.168.1.30", "192.168.137.1"])
    assert network.lan_ips() == ["192.168.1.30", "192.168.137.1"]


@pytest.mark.skipif(sys.platform == "win32", reason="interfaces come from the host name there")
def test_this_machines_interfaces_include_its_route_address() -> None:
    primary = network.primary_ip()
    if primary is None:
        pytest.skip("no network")
    names = {ip: name for name, ip in network.interface_ips()}
    assert primary in names
    assert "127.0.0.1" in names


def test_the_share_link_uses_a_reachable_address(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_net(monkeypatch, [("rmnet0", "10.71.3.9"), ("ap0", "192.168.43.1")], primary="10.71.3.9")
    a = make_node("Alpha")
    urls = owner(a)("GET", "/api/info")[1]
    assert "10.71.3.9" not in str(urls)
    assert f"http://192.168.43.1:{a.port}" in str(urls)


# ----------------------------------------------------- sweep, not multicast


def test_neighbours_are_the_private_slash_24_around_each_address() -> None:
    hosts = neighbours(["192.168.43.7", "10.0.5.1", "8.8.4.4"])
    assert "192.168.43.1" in hosts
    assert "192.168.43.254" in hosts
    assert "192.168.43.7" not in hosts  # ourselves
    assert "10.0.5.200" in hosts
    assert not any(h.startswith("8.8.") for h in hosts)  # never outside private networks
    assert len(hosts) == 253 + 253


def test_a_paired_device_that_moved_is_found_without_multicast(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    pair(a, b)
    peer = a.mesh._peers[b.mesh.id]
    peer.host, peer.last_seen = "192.0.2.9", 0.0  # where it was; it's away now
    here = lan_ip()  # its new lease (loopback never counts as a move)
    monkeypatch.setattr(network, "neighbours", lambda ips: [here])
    a.mesh.sweep()
    wait_for(lambda: a.mesh._peers[b.mesh.id].host == here and peer.online, timeout=5)
    assert [p.id for p in a.mesh._peers.values()].count(b.mesh.id) == 1  # no duplicate


def test_a_spoofer_found_by_a_sweep_doesnt_move_a_paired_device(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An app at the swept address claims to be Bravo but can't prove it.
    a, b, c = make_node("Alpha"), make_node("Bravo"), make_node("Mallory")
    pair(a, b)
    real = c.mesh.self_info
    monkeypatch.setattr(
        c.mesh, "self_info", lambda **kw: {**real(**kw), "id": b.mesh.id, "port": c.port}
    )
    peer = a.mesh._peers[b.mesh.id]
    peer.host, peer.port = "192.0.2.9", b.port
    monkeypatch.setattr(network, "neighbours", lambda ips: [lan_ip()])
    monkeypatch.setattr(a.mesh, "_sweep_ports", lambda: {c.port})
    challenged: list[str] = []
    real_check = a.mesh._check_move
    monkeypatch.setattr(
        a.mesh, "_check_move", lambda *args: (challenged.append(args[1]), real_check(*args))
    )
    a.mesh.sweep()
    wait_for(lambda: challenged == [lan_ip()])  # it was found and asked to prove it…
    __import__("time").sleep(1.0)
    assert peer.host == "192.0.2.9"  # …and couldn't


def test_several_paired_devices_on_one_hotspot_are_found_by_one_sweep(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase L: everyone moved to the phone's hotspot (new leases), and it drops multicast."""
    a, b, c = make_node("Alpha"), make_node("Bravo"), make_node("Charlie")
    pair(a, b)
    pair(a, c)
    for other in (b, c):
        peer = a.mesh._peers[other.mesh.id]
        peer.host, peer.last_seen = "192.0.2.9", 0.0  # their old addresses; away
    here = lan_ip()
    monkeypatch.setattr(network, "neighbours", lambda ips: [here])
    a.mesh.sweep()
    for other in (b, c):
        wait_for(lambda o=other: a.mesh._peers[o.mesh.id].host == here, timeout=5)
    ids = [p.id for p in a.mesh._peers.values()]
    assert ids.count(b.mesh.id) == ids.count(c.mesh.id) == 1


def test_pairing_by_code_finds_the_device_when_multicast_is_blocked(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")  # discovery off: no multicast at all
    code = owner(b)("POST", "/api/pair/open")[1]["code"]
    monkeypatch.setattr(network, "neighbours", lambda ips: ["127.0.0.1"])
    monkeypatch.setattr(a.mesh, "_sweep_ports", lambda: {a.port, b.port})
    status, data = owner(a)("POST", "/api/pair", {"code": code})
    assert status == 200, data
    assert data["device"]["name"] == "Bravo"


def test_a_network_change_starts_a_sweep_when_a_paired_device_is_away(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    pair(a, b)
    a.mesh._peers[b.mesh.id].last_seen = 0.0
    swept: list[bool] = []
    monkeypatch.setattr(a.mesh, "sweep", lambda: swept.append(True))
    monkeypatch.setattr(network, "lan_ips", lambda: ["192.168.1.20"])
    a.mesh.check_lan()  # started on a network where Bravo isn't heard: look for it
    assert swept == [True]
    a.mesh.check_lan()  # same network: nothing new
    assert swept == [True]
    monkeypatch.setattr(network, "lan_ips", lambda: ["192.168.43.77"])  # joined the hotspot
    a.mesh.check_lan()
    assert swept == [True, True]
    a.mesh._peers[b.mesh.id].last_seen = __import__("time").monotonic()  # Bravo is here
    monkeypatch.setattr(network, "lan_ips", lambda: ["10.0.0.4"])
    a.mesh.check_lan()  # nobody paired is away: no need
    assert swept == [True, True]


# --------------------------------------------- isolation vs. Open Transfer


@pytest.mark.parametrize(
    ("error", "says"),
    [
        (ConnectionRefusedError(), "isn’t open"),
        (TimeoutError(), "isolation"),
        (OSError(errno.EHOSTUNREACH, "No route to host"), "isolation"),
    ],
)
def test_an_unreachable_address_says_why(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch, error: OSError, says: str
) -> None:
    a = make_node("Alpha")

    def fail(*_a: object, **_k: object) -> None:
        raise error

    monkeypatch.setattr(a.mesh, "_http", fail)
    with pytest.raises(MeshError) as caught:
        a.mesh.connect("192.168.43.1:5000")
    assert says in str(caught.value)
    if sys.platform == "darwin" and says == "isolation":
        assert "Local Network" in str(caught.value)


def test_a_peer_is_one_device_whatever_its_address(make_node: NodeFactory) -> None:
    # Moving between Wi-Fi and a hotspot never adds a second tile.
    a = make_node("Alpha")
    info = {"id": "d-" + "1" * 24, "name": "Phone", "port": 5000, "form": "phone"}
    a.mesh._upsert_peer(info, "192.168.1.50")
    a.mesh._upsert_peer(info, "192.168.43.1")
    a.mesh._upsert_peer(info, "10.42.17.1")
    assert [p for p in a.mesh._peers.values() if isinstance(p, Peer)] == [
        a.mesh._peers["d-" + "1" * 24]
    ]
    assert a.mesh._peers["d-" + "1" * 24].host == "10.42.17.1"
