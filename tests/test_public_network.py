"""Windows blocks other devices from reaching apps on a network set to "Public" —
the usual reason a Mac or phone sees a Windows PC but can't pair or send to it.
The PC's own app notices and says how to fix it."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from open_transfer import mesh as mesh_module
from open_transfer import network
from tests.conftest import NodeFactory
from tests.test_mesh import owner, wait_for


def answer(stdout: str):  # type: ignore[no-untyped-def]
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_public_wifi_is_found_on_windows() -> None:
    run = answer("Home-5G\r\n\r\n")
    assert network.public_networks(run=run, platform="win32") == ["Home-5G"]
    assert "Get-NetConnectionProfile" in " ".join(run.calls[0])  # type: ignore[attr-defined]


def test_private_networks_are_fine() -> None:
    assert network.public_networks(run=answer(""), platform="win32") == []


def test_other_systems_are_not_asked() -> None:
    run = answer("x")
    assert network.public_networks(run=run, platform="darwin") == []
    assert run.calls == []  # type: ignore[attr-defined]


def test_a_failing_check_is_not_an_error() -> None:
    def broken(*_a: Any, **_k: Any) -> Any:
        raise OSError("no powershell")

    assert network.public_networks(run=broken, platform="win32") == []


def test_the_owner_is_told(make_node: NodeFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mesh_module.network, "public_networks", lambda: ["Home-5G"])
    node = make_node("Sonu-PC", device_platform="windows")
    node.mesh.check_network()
    assert owner(node).state()["network"] == {"public": ["Home-5G"]}
    monkeypatch.setattr(mesh_module.network, "public_networks", lambda: [])
    node.mesh.check_network()
    wait_for(lambda: owner(node).state()["network"] == {"public": []})
