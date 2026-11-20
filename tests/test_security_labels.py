"""What each transfer is told it is: one level for the weakest hop it crossed.

* ``verified``: encrypted and authenticated (paired apps).
* ``unverified``: encrypted, but nobody vouched for the other end (unpaired
  apps; a WebRTC transfer whose connection messages crossed a plain hop).
* ``transport``: encrypted on each hop (HTTPS to a browser, DTLS between
  browsers), so the app in between can read it and a browser trusts its app
  only as far as the certificate it accepted.
* ``plain``: something crossed the network unencrypted (plain HTTP, an older app).
* ``local``: never left this device.
"""

from __future__ import annotations

import http.cookiejar
import ssl
import urllib.request
from typing import Any

from open_transfer.node import Node
from tests.conftest import NodeFactory
from tests.test_encrypted_transfers import paired
from tests.test_mesh import (
    Client,
    connect,
    device_id,
    incoming,
    lan_ip,
    owner,
    send,
    target_states,
    visitor,
    wait_for,
)


def tls_visitor(node: Node, name: str, form: str = "phone") -> Client:
    """A browser on ``node``'s HTTPS port (it accepted the self-signed certificate)."""
    client = Client(f"https://{lan_ip()}:{node.https_port}")
    context = ssl.create_default_context()
    context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    client._opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        urllib.request.HTTPSHandler(context=context),
    )
    status, _ = client("POST", "/api/me", {"name": name, "form": form, "platform": "android"})
    assert status == 200
    return client


def security(client: Client, job: str) -> dict[str, str]:
    for item in client.state()["outgoing"]:
        if item["id"] == job:
            return {t["name"]: t["security"] for t in item["targets"]}
    raise AssertionError(job)


def accepted(sender: Client, receiver: Client, to: str, name: str = "x.txt") -> tuple[str, Any]:
    job = send(sender, [to], [(name, b"x")])
    offer = incoming(receiver)
    receiver("POST", f"/api/incoming/{offer['id']}/accept")
    wait_for(lambda: set(target_states(sender, job).values()) == {"accepted"})
    return job, incoming(receiver, "accepted")


def test_paired_apps_are_encrypted_and_authenticated(make_node: NodeFactory) -> None:
    a, b = paired(make_node)
    job = send(owner(a), [b.mesh.id], [("p.txt", b"p")])
    wait_for(lambda: security(owner(a), job) == {"Bravo": "verified"})
    assert incoming(owner(b), "accepted")["security"] == "verified"


def test_unpaired_apps_are_encrypted_not_verified(make_node: NodeFactory) -> None:
    a, b = make_node("Alpha"), make_node("Bravo")
    connect(a, b)
    job, offer = accepted(owner(a), owner(b), device_id(owner(a), "Bravo"))
    assert security(owner(a), job) == {"Bravo": "unverified"}
    assert offer["security"] == "unverified"


def test_an_older_app_is_plain(make_node: NodeFactory) -> None:
    a = make_node("Alpha", encrypted_only=False)
    b = make_node("Bravo", encrypted_only=False)  # an older app has no such setting
    real = b.mesh.self_info
    b.mesh.self_info = lambda **kw: {k: v for k, v in real(**kw).items() if k not in {"enc", "kx"}}  # type: ignore[method-assign]
    connect(a, b)
    job, _ = accepted(owner(a), owner(b), device_id(owner(a), "Bravo"))
    assert security(owner(a), job) == {"Bravo": "plain"}


def test_a_browser_on_plain_http_makes_its_transfer_plain(make_node: NodeFactory) -> None:
    a, b = paired(make_node)
    phone = visitor(a, "Pixel")
    job = send(phone, [b.mesh.id], [("v.txt", b"v")])
    wait_for(lambda: security(phone, job) == {"Bravo": "plain"})  # its own hop is readable
    assert incoming(owner(b))["security"] == "plain"  # a browser's offer waits to be accepted


def test_a_browser_over_https_is_encrypted_transport(make_node: NodeFactory) -> None:
    a = make_node("Alpha", https=True)
    b = make_node("Bravo")
    from tests.test_moves import pair

    pair(a, b)
    wait_for(lambda: a.mesh.trust.get(b.mesh.id).enc)  # type: ignore[union-attr]
    phone = tls_visitor(a, "Pixel")
    job = send(phone, [b.mesh.id], [("t.txt", b"t")])
    wait_for(lambda: security(phone, job) == {"Bravo": "transport"})
    assert incoming(owner(b))["security"] == "transport"


def test_an_app_to_its_own_browser_is_as_good_as_that_browsers_hop(
    make_node: NodeFactory,
) -> None:
    plain_app, tls_app = make_node("Alpha"), make_node("Bravo", https=True)
    for app, client, expected in (
        (plain_app, visitor(plain_app, "Phone"), "plain"),
        (tls_app, tls_visitor(tls_app, "Phone"), "transport"),
    ):
        job, offer = accepted(owner(app), client, device_id(owner(app), "Phone"))
        assert security(owner(app), job) == {"Phone": expected}
        assert offer["security"] == expected


def test_an_app_to_a_browser_behind_a_paired_app(make_node: NodeFactory) -> None:
    a = make_node("Alpha")
    b = make_node("Bravo", https=True)
    from tests.test_moves import pair

    pair(a, b)
    wait_for(lambda: a.mesh.trust.get(b.mesh.id).enc)  # type: ignore[union-attr]
    tab = tls_visitor(b, "Tab", form="tablet")
    job, _ = accepted(owner(a), tab, device_id(owner(a), "Tab"))
    # Alpha → Bravo verified, Bravo → the tab over TLS: encrypted transport, said by Bravo.
    assert security(owner(a), job) == {"Tab": "transport"}


def test_a_direct_transfer_over_plain_hops_is_encrypted_not_verified(
    make_node: NodeFactory,
) -> None:
    a = make_node("Alpha")
    phone, tablet = visitor(a, "Phone"), visitor(a, "Tablet")
    job, _ = accepted(phone, tablet, device_id(phone, "Tablet"))
    tablet_id = tablet.state()["me"]["id"]
    phone("POST", f"/api/send/{job}/targets/{tablet_id}/direct", {"state": "trying"})
    # DTLS between the browsers, but its fingerprints crossed plain HTTP.
    assert security(phone, job) == {"Tablet": "unverified"}


def test_with_https_browsers_cant_use_the_plain_port(make_node: NodeFactory) -> None:
    a = make_node("Alpha", https=True)
    plain = Client(f"http://{lan_ip()}:{a.port}")
    status, data = plain("GET", "/api/state")
    assert status == 403
    assert data["error"]["code"] == "https_required"
    assert f"https://{lan_ip()}:{a.https_port}" in data["error"]["message"]
    assert tls_visitor(a, "Pixel")("GET", "/api/state")[0] == 200
    assert owner(a)("GET", "/api/state")[0] == 200  # the device's own window
    assert plain("GET", "/api/p2p/v1/info")[0] == 200  # apps, encrypted their own way
    assert plain("GET", "/api/health")[0] == 200  # health checks (Docker) carry nothing
