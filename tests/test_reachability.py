"""A device we hear on the Wi-Fi but can't connect to — almost always its firewall
(Windows with the network set to Public, or "Private networks" not allowed). The
app says so, by name, instead of "No device is showing that code"."""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

from open_transfer.mesh import MeshError
from tests.conftest import NodeFactory
from tests.test_mesh import owner

WINDOWS = {
    "id": "d-" + "cd" * 12,
    "name": "Sonu-PC",
    "port": 4242,
    "form": "computer",
    "platform": "windows",
}


class Discovery:
    """Multicast that works one way: we hear announcements, nobody answers our query."""

    working = True

    def find(self) -> None:
        pass


def blocked(*_args: Any, **_kwargs: Any) -> tuple[int, dict[str, Any]]:
    raise ConnectionRefusedError("blocked by its firewall")


def heard_but_blocked(make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    mac = make_node("Mac")
    peer = mac.mesh._upsert_peer(WINDOWS, "192.168.1.37")
    assert peer is not None
    monkeypatch.setattr(mac.mesh, "_http", blocked)
    assert mac.mesh._hello(peer) is False
    return mac


def entry(node, device_id: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return next(d for d in owner(node).state()["devices"] if d["id"] == device_id)


def test_a_device_that_does_not_answer_is_marked(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    mac = heard_but_blocked(make_node, monkeypatch)
    device = entry(mac, WINDOWS["id"])
    assert device["online"] is True  # we do hear it
    assert device["reachable"] is False


def test_it_recovers_once_it_answers(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    mac = heard_but_blocked(make_node, monkeypatch)
    monkeypatch.setattr(mac.mesh, "_http", lambda *a, **k: (200, WINDOWS))
    assert mac.mesh._hello(mac.mesh._peers[WINDOWS["id"]]) is True
    assert entry(mac, WINDOWS["id"])["reachable"] is True


def test_devices_that_answer_are_reachable(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    a.mesh.connect(f"127.0.0.1:{b.port}")
    assert entry(a, b.mesh.id)["reachable"] is True


def test_pairing_names_the_device_its_firewall_blocks(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    mac = heard_but_blocked(make_node, monkeypatch)
    mac.mesh.discovery = Discovery()  # type: ignore[assignment]
    with pytest.raises(MeshError) as caught:
        mac.mesh.pair_with("822328")
    error = caught.value
    assert error.code == "code_not_found"  # the page still opens the address field
    message = str(error)
    assert "Sonu-PC" in message
    assert "firewall" in message
    assert "Private" in message


def test_pairing_without_such_a_device_keeps_the_plain_message(make_node: NodeFactory) -> None:
    mac = make_node("Mac")
    mac.mesh.discovery = Discovery()  # type: ignore[assignment]
    with pytest.raises(MeshError, match="No device on this network is showing a pairing code"):
        mac.mesh.pair_with("822328")


# The other side of it: the PC learns from those who can't reach it. Its own hello
# gets through (outgoing connections are allowed), and the answer says so.


def pc_and_mac(make_node: NodeFactory):  # type: ignore[no-untyped-def]
    pc, mac = make_node("Sonu-PC"), make_node("Mac")
    pc.mesh.connect(f"127.0.0.1:{mac.port}")  # the PC reaches the Mac
    assert pc.mesh._hello(pc.mesh._peers[mac.mesh.id])  # and says hello, so the Mac knows it
    return pc, mac


def mac_cannot_reach(pc, mac) -> None:  # type: ignore[no-untyped-def]
    mac.mesh._peers[pc.mesh.id].reachable = False  # what a dropped connection leaves


def test_a_device_that_cant_reach_us_says_so(make_node: NodeFactory) -> None:
    pc, mac = pc_and_mac(make_node)
    mac_cannot_reach(pc, mac)
    assert pc.mesh._hello(pc.mesh._peers[mac.mesh.id]) is True
    network = owner(pc).state()["network"]
    assert network["unreachable_by"] == ["Mac"]
    assert network["reachable_by"] == 0


def test_once_it_can_reach_us_the_warning_goes(make_node: NodeFactory) -> None:
    pc, mac = pc_and_mac(make_node)
    mac_cannot_reach(pc, mac)
    pc.mesh._hello(pc.mesh._peers[mac.mesh.id])
    mac.mesh._peers[pc.mesh.id].reachable = True
    pc.mesh._hello(pc.mesh._peers[mac.mesh.id])
    network = owner(pc).state()["network"]
    assert network["unreachable_by"] == []
    assert network["reachable_by"] == 1


def test_only_devices_still_here_count(make_node: NodeFactory) -> None:
    pc, mac = pc_and_mac(make_node)
    mac_cannot_reach(pc, mac)
    pc.mesh._hello(pc.mesh._peers[mac.mesh.id])
    pc.mesh._peers[mac.mesh.id].last_seen = 0  # the Mac left
    assert owner(pc).state()["network"]["unreachable_by"] == []


# "Allow on this network": one narrow inbound rule for this app, on the network type
# in use, through one Windows admin (UAC) prompt — only when the owner asks.


def _script(cmd: list[str]) -> str:
    import base64

    encoded = cmd[cmd.index("-ArgumentList") + 1].split("'-EncodedCommand','")[1].rstrip("';")
    return base64.b64decode(encoded).decode("utf-16-le")


def test_the_firewall_rule_is_narrow() -> None:
    from open_transfer import network

    cmd = network.allow_command(r"C:\Users\o'neil\Desktop\open-transfer.exe", 5000, "Private")
    assert cmd[:2] == ["powershell", "-NoProfile"]
    assert "-Verb RunAs" in cmd[-1]  # Windows asks the person (UAC)
    assert cmd[-1].endswith("exit $p.ExitCode")  # the elevated copy's result, not Start-Process's
    script = _script(cmd[-1].split())
    assert script.startswith("$ErrorActionPreference = 'Stop';")  # a refused rule fails
    assert "-Program 'C:\\Users\\o''neil\\Desktop\\open-transfer.exe'" in script  # quoted safely
    assert "-Profile Private" in script
    assert "-Protocol TCP -LocalPort 5000" in script
    assert "-Protocol UDP -LocalPort 47823" in script  # nearby devices' announcements
    assert "-Action Allow" in script
    assert "Set-NetFirewallProfile" not in script  # never touches the firewall itself
    assert "Enabled False" not in script


@pytest.mark.parametrize(
    ("returncode", "stderr", "result"),
    [
        (0, "", "allowed"),
        (1, "The operation was canceled by the user.", "cancelled"),
        (1, "boom", "failed"),
    ],
)
def test_allowing_reports_what_happened(returncode: int, stderr: str, result: str) -> None:
    from types import SimpleNamespace

    from open_transfer import network

    def run(*_a: Any, **_k: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    assert network.allow_firewall(r"C:\app.exe", 5000, "Private", run=run) == result


def test_only_the_owner_of_the_windows_app_can_allow(
    make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_transfer import network
    from tests.test_mesh import visitor

    pc = make_node("Sonu-PC")
    status, data = owner(pc)("POST", "/api/network/allow")
    assert status == 501  # not the packaged Windows app (never python.exe)
    assert data["error"]["code"] == "unsupported"
    assert owner(pc).state()["network"]["can_allow"] is False

    asked: list[tuple[Any, ...]] = []
    monkeypatch.setattr(network, "can_allow_firewall", lambda: True)
    monkeypatch.setattr(network, "allow_firewall", lambda *a, **k: asked.append(a) or "allowed")
    assert owner(pc).state()["network"]["can_allow"] is True
    status, _ = visitor(pc, "Phone")("POST", "/api/network/allow")
    assert status == 403
    assert asked == []
    status, data = owner(pc)("POST", "/api/network/allow")
    assert (status, data) == (200, {"result": "allowed"})
    assert asked[0][1:] == (pc.port, "Private")


# The real thing, on GitHub's Windows runners only (admin, no UAC prompt): never on
# someone's own PC, where it would change their firewall.
on_windows_ci = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true",
    reason="changes the firewall: GitHub's Windows runners only",
)


def _ps(script: str) -> str:
    import subprocess

    done = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],  # noqa: S607 - Windows PowerShell
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout


def _rules(program: str) -> list[str]:
    """Each rule for ``program`` (quoted for PowerShell): "<profile> <action> <proto> <port>"."""
    out = _ps(
        f"Get-NetFirewallApplicationFilter -Program '{program}' | Get-NetFirewallRule | "
        'ForEach-Object { "$($_.Profile) $($_.Action) " + ($_ | Get-NetFirewallPortFilter | '
        'ForEach-Object { "$($_.Protocol) $($_.LocalPort)" }) }'
    )
    return sorted(line.strip() for line in out.splitlines() if line.strip())


def _remove(program: str) -> None:
    _ps(
        f"Get-NetFirewallApplicationFilter -Program '{program}' | Get-NetFirewallRule | "
        "Remove-NetFirewallRule"
    )


@on_windows_ci
def test_windows_really_gets_the_narrow_rule(tmp_path: Any) -> None:
    from open_transfer import network

    program = str(tmp_path / "o'neil" / "open-transfer.exe")
    try:
        assert network.allow_firewall(program, 5123, "Private") == "allowed"
        assert _rules(program.replace("'", "''")) == [
            "Private Allow TCP 5123",
            "Private Allow UDP 47823",
        ]
    finally:
        _remove(program.replace("'", "''"))


@on_windows_ci
def test_a_rule_windows_refuses_is_not_reported_as_allowed(tmp_path: Any) -> None:
    from open_transfer import network

    program = str(tmp_path / "open-transfer.exe")
    try:
        assert network.allow_firewall(program, 5123, "NoSuchProfile") == "failed"
    finally:
        _remove(program)
