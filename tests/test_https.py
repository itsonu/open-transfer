"""--https: browsers reach the app over TLS (docs/encryption.md, "Browsers").

A page on http://<LAN address> is not a secure context and shares no key with
the app, so a browser's own hop can only be protected by TLS. The certificate
is self-signed; its key is sealed in the vault like the pair keys.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
import urllib.request
from pathlib import Path

import pytest

from open_transfer import network
from open_transfer.config import Config
from open_transfer.node import Node
from tests.conftest import NodeFactory
from tests.test_mesh import owner


def fetch_tls(port: int, path: str = "/") -> tuple[int, bytes, bytes]:
    """GET over TLS without checking the (self-signed) certificate; returns its DER too."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    with (
        socket.create_connection(("127.0.0.1", port), timeout=10) as raw,
        context.wrap_socket(raw, server_hostname="localhost") as tls,
    ):
        der = tls.getpeercert(binary_form=True) or b""
        tls.sendall(request.encode())
        data = b""
        while chunk := tls.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), body, der


def fingerprint(der: bytes) -> str:
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


def test_browsers_get_the_page_over_tls(make_node: NodeFactory) -> None:
    node = make_node("Alpha", https=True)
    assert node.https_port
    status, body, der = fetch_tls(node.https_port)
    assert status == 200
    assert b"<html" in body.lower()
    # Apps keep talking to each other on the plain port (their own encryption).
    assert urllib.request.urlopen(node.local_url + "/api/health").status == 200
    state = owner(node).state()
    assert state["https"] == {"port": node.https_port, "fingerprint": fingerprint(der)}


def test_the_link_and_qr_code_point_at_https(make_node: NodeFactory) -> None:
    node = make_node("Alpha", https=True)
    info = owner(node)("GET", "/api/info")[1]
    assert info["share_url"].startswith("https://")
    assert info["share_url"].endswith(f":{node.https_port}")
    assert all(url.startswith("https://") for url in info["urls"])


def test_without_https_nothing_changes(make_node: NodeFactory) -> None:
    node = make_node("Alpha")
    assert node.https_port == 0
    assert "https" not in owner(node).state()
    assert owner(node)("GET", "/api/info")[1]["share_url"].startswith("http://")


def test_the_certificate_is_kept_and_its_key_sealed(tmp_path: Path) -> None:
    def start() -> Node:
        node = Node(Config(storage_dir=tmp_path / "share", port=0, discovery=False, https=True))
        node.start()
        return node

    first = start()
    try:
        _, _, der = fetch_tls(first.https_port)
    finally:
        first.stop()
    state = tmp_path / "share" / ".open-transfer"
    saved = json.loads((state / "tls.json").read_text())
    assert saved["key"].startswith("ot-vault/1:")
    assert b"PRIVATE KEY" not in b"".join(p.read_bytes() for p in state.rglob("*") if p.is_file())
    again = start()  # same certificate: a browser's "proceed" for it still holds
    try:
        assert fetch_tls(again.https_port)[2] == der
    finally:
        again.stop()


def test_the_certificate_names_this_device(make_node: NodeFactory) -> None:
    from cryptography import x509

    node = make_node("Alpha", https=True)
    cert = x509.load_der_x509_certificate(fetch_tls(node.https_port)[2])
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    ips = {str(ip) for ip in names.get_values_for_type(x509.IPAddress)}
    assert "127.0.0.1" in ips
    assert set(network.lan_ips()) <= ips
    assert "localhost" in names.get_values_for_type(x509.DNSName)


def test_a_long_hostname_still_gets_a_certificate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cryptography import x509

    from open_transfer import tls

    monkeypatch.setattr(network, "hostname", lambda: "runner-" + "x" * 70)  # CI Macs have these
    cert, _ = tls.certificate(tmp_path)
    name = x509.load_pem_x509_certificate(cert).subject.rfc4514_string()
    assert len(name) <= len("CN=") + 64


def test_a_failed_https_start_leaves_nothing_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import threading

    from open_transfer import tls

    def broken(*_: object) -> None:
        raise ValueError("no certificate")

    monkeypatch.setattr(tls, "certificate", broken)
    before = set(threading.enumerate())
    node = Node(Config(storage_dir=tmp_path / "share", port=0, discovery=False, https=True))
    with pytest.raises(ValueError, match="no certificate"):
        node.start()
    leftover = [t for t in set(threading.enumerate()) - before if not t.daemon and t.is_alive()]
    assert not leftover  # non-daemon worker threads would keep the process from exiting


def test_a_browser_on_the_plain_port_is_sent_to_https(make_node: NodeFactory) -> None:
    from tests.test_mesh import lan_ip

    node = make_node("Alpha", https=True)
    host = lan_ip()
    conn = http.client.HTTPConnection(host, node.port, timeout=5)
    conn.request("GET", "/?x=1", headers={"Accept": "text/html", "Host": f"{host}:{node.port}"})
    res = conn.getresponse()
    assert res.status == 302
    assert res.getheader("Location") == f"https://{host}:{node.https_port}/?x=1"
    # The device's own window and the apps' own API stay where they are.
    assert urllib.request.urlopen(node.local_url + "/").status == 200
    conn = http.client.HTTPConnection(host, node.port, timeout=5)
    conn.request("GET", "/api/p2p/v1/info", headers={"Host": f"{host}:{node.port}"})
    assert conn.getresponse().status == 200
