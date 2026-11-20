from __future__ import annotations

import http.client
import os
import random
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from open_transfer.app import create_app
from open_transfer.config import Config
from open_transfer.node import Node

AppFactory = Callable[..., Flask]

# Never the real Keychain / DPAPI / keyring from tests (servers started by session
# fixtures too); tests/test_keystore.py tests those on purpose.
os.environ.setdefault("OPEN_TRANSFER_KEYSTORE", "file")


@pytest.fixture(autouse=True)
def _no_sweeping_real_networks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A paired test app that is away would make another sweep the machine's real /24."""
    from open_transfer import network

    monkeypatch.setattr(network, "neighbours", lambda ips: [])


@pytest.fixture
def make_app(tmp_path: Path) -> AppFactory:
    def factory(**overrides: object) -> Flask:
        overrides.setdefault("storage_dir", tmp_path / "share")
        overrides.setdefault("reserve_disk_bytes", 0)
        # The classic suite tests the shared-folder mode as seen by a visitor;
        # tests/test_mesh*.py cover the owner and nearby devices.
        overrides.setdefault("share_folder", True)
        overrides.setdefault("allow_delete", True)  # opt-in since 2026-10-31; tested here
        overrides.setdefault("owner_loopback", False)
        overrides.setdefault("discovery", False)
        app = create_app(Config(**overrides))  # type: ignore[arg-type]
        app.config["TESTING"] = True
        return app

    return factory


@pytest.fixture
def app(make_app: AppFactory) -> Flask:
    return make_app()


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def share_dir(app: Flask) -> Path:
    return Path(app.extensions["open_transfer"]["config"].storage_dir)


def upload(client: FlaskClient, name: str, data: bytes = b"hello", **headers: str):
    from urllib.parse import quote

    return client.post(
        "/api/files",
        data=data,
        headers={"X-Filename": quote(name), "Content-Type": "application/octet-stream", **headers},
    )


# Real nodes on 127.0.0.1 (each with its own folder) for app-to-app tests.
NodeFactory = Callable[..., Node]


@pytest.fixture
def make_node(tmp_path: Path) -> Iterator[NodeFactory]:
    nodes: list[Node] = []
    discovery_port = random.randint(40000, 59000)

    def factory(name: str, **overrides: Any) -> Node:
        overrides.setdefault("discovery", False)
        overrides.setdefault("discovery_port", discovery_port)
        config = Config(
            storage_dir=tmp_path / name,
            port=0,
            device_name=name,
            reserve_disk_bytes=0,
            **overrides,
        )
        node = Node(config)
        node.start()
        nodes.append(node)
        return node

    yield factory
    for node in nodes:
        node.stop()


# A wiretap on ``http.client``: every byte an app sends to another app's port,
# what anyone on the Wi-Fi would see.
class Tap:
    def __init__(self) -> None:
        self.sent: dict[int, bytearray] = defaultdict(bytearray)
        self.flip_at: dict[int, int] = {}  # port → byte offset (in big bodies) to corrupt

    def to(self, node: Node) -> bytes:
        return bytes(self.sent[node.port])


@pytest.fixture
def tap(monkeypatch: pytest.MonkeyPatch) -> Tap:
    tap = Tap()
    real = http.client.HTTPConnection.send

    def send(conn: http.client.HTTPConnection, data: Any) -> None:
        if isinstance(data, (bytes, bytearray)):
            if conn.port in tap.flip_at and len(data) > 60_000:  # a body block, not headers
                at = min(tap.flip_at.pop(conn.port), len(data) - 1)
                data = bytes(data[:at]) + bytes([data[at] ^ 1]) + bytes(data[at + 1 :])
            tap.sent[conn.port] += data
        real(conn, data)

    monkeypatch.setattr(http.client.HTTPConnection, "send", send)
    return tap
