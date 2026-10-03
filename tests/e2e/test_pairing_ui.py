"""Pairing in the real page: show a code, type it on the other device, Allow there."""

from __future__ import annotations

from tests import test_mesh
from tests.e2e.conftest import sync_api
from tests.test_mesh import NodeFactory, connect

expect = sync_api.expect
make_node = test_mesh.make_node  # the shared pytest fixture


def _open_code(pg, url: str) -> str:  # type: ignore[no-untyped-def]
    pg.goto(url)
    pg.click("#connect-button")
    expect(pg.locator("#pair-code[data-open='1']")).to_be_visible()  # only a live code is shown
    return str(pg.locator("#pair-code").inner_text()).replace(" ", "")


def _enter(pg, url: str, code: str, target_id: str) -> None:  # type: ignore[no-untyped-def]
    pg.goto(url)
    pg.click("#connect-button")
    pg.click("[role=tab][data-tab=enter]")
    pg.fill("#pair-input", code)
    pg.select_option("#pair-target", value=target_id)  # the device itself, not an address
    pg.click("#pair-submit")


def test_pairing_needs_the_other_owner_to_allow(make_node: NodeFactory, browser) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo = make_node("Alpha"), make_node("Bravo")
    connect(alpha, bravo)  # visible over HTTP only: no multicast involved
    shows, enters = browser.new_page(), browser.new_page()
    code = _open_code(shows, bravo.local_url)
    _enter(enters, alpha.local_url, code, bravo.mesh.id)

    expect(shows.locator("#pair-request-title")).to_have_text("Pair with Alpha?", timeout=10_000)
    expect(enters.locator("#pair-status")).to_contain_text("allow")
    shows.click("#pair-request-allow")
    expect(enters.locator(".toast", has_text="Paired with Bravo")).to_be_visible(timeout=10_000)
    expect(shows.locator(".toast", has_text="Paired with Alpha")).to_be_visible(timeout=10_000)


def test_denied_pairing_says_why(make_node: NodeFactory, browser) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo = make_node("Alpha"), make_node("Bravo")
    connect(alpha, bravo)
    shows, enters = browser.new_page(), browser.new_page()
    code = _open_code(shows, bravo.local_url)
    _enter(enters, alpha.local_url, code, bravo.mesh.id)
    expect(shows.locator("#pair-request-title")).to_have_text("Pair with Alpha?", timeout=10_000)
    shows.click("#pair-request-deny")
    expect(enters.locator("#pair-error")).to_contain_text("didn’t allow", timeout=10_000)
    expect(enters.locator("#pair-error")).to_contain_text("Ask its owner to press Allow")
    assert not bravo.mesh.trust.ids()
