"""Browser tests for the Clipboard sync sheet: turning sync on, pausing,
choosing devices and how, and removing a device from the network."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.test_clipboard import call
from tests.e2e.test_ui import Server, _start, sync_api

pytestmark = pytest.mark.e2e
expect = sync_api.expect


@pytest.fixture
def two_apps(tmp_path: Path) -> Iterator[tuple[Server, Server]]:
    gens = [_start(tmp_path, name="Alpha", owner=True), _start(tmp_path, name="Bravo", owner=True)]
    alpha, bravo = (next(g) for g in gens)
    code = call(bravo, "GET", "/api/state")["pairing"]["code"]
    port = bravo.url.rsplit(":", 1)[1]
    call(alpha, "POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{port}"})
    yield alpha, bravo
    for gen in gens:
        for _ in gen:
            pass


def test_the_sync_sheet(two_apps: tuple[Server, Server], page) -> None:  # type: ignore[no-untyped-def]
    alpha, _bravo = two_apps
    page.goto(alpha.url)
    expect(page.locator("#clipboard-section")).to_be_visible(timeout=10_000)
    page.click("#sync-open")
    sheet = page.locator("#sync-dialog")
    expect(sheet).to_be_visible()
    expect(page.locator("#sync-status")).to_have_text("Off")

    page.locator("#sync-enabled").check()
    # A command-line app has no clipboard agent: the sheet says so.
    expect(page.locator("#sync-status")).to_contain_text("can’t reach its clipboard")
    assert call(alpha, "GET", "/api/sync")["active"] is True
    device = page.locator("#sync-devices li", has_text="Bravo")
    expect(device.get_by_role("switch", name="Sync with Bravo")).to_be_checked()
    expect(page.locator("#clip-sync-state")).to_have_text("On")

    page.click("#sync-pause")
    expect(page.locator("#sync-status")).to_contain_text("Paused until")
    page.click("#sync-resume")
    expect(page.locator("#sync-status")).not_to_contain_text("Paused")

    device.get_by_role("switch", name="Sync with Bravo").uncheck()
    expect(device.get_by_role("switch", name="Sync with Bravo")).not_to_be_checked()
    status = call(alpha, "GET", "/api/sync")
    assert status["devices"][0]["excluded"] is True

    page.select_option("#sync-mode", "manual")
    expect(page.locator("#sync-mode")).to_have_value("manual")
    assert call(alpha, "GET", "/api/sync")["policy"]["mode"] == "manual"
    page.select_option("#sync-expiry", "600")
    page.locator(".sync-types input[value='url']").uncheck()
    expect(page.locator(".sync-types input[value='url']")).not_to_be_checked()
    policy = call(alpha, "GET", "/api/sync")["policy"]
    assert policy["expiry"] == 600
    assert policy["types"] == ["text"]

    device.get_by_role("button", name="Remove Bravo from your devices").click()
    expect(page.locator("#confirm-dialog")).to_be_visible()
    page.click("#confirm-ok")
    expect(page.locator("#sync-devices")).to_have_count(0)
    assert call(alpha, "GET", "/api/sync")["devices"] == []


def test_the_sheet_works_on_a_phone(two_apps: tuple[Server, Server], browser) -> None:  # type: ignore[no-untyped-def]
    alpha, _ = two_apps
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
    page = context.new_page()
    try:
        page.goto(alpha.url)
        expect(page.locator("#clipboard-section")).to_be_visible(timeout=10_000)
        page.click("#sync-open")
        expect(page.locator("#sync-enabled")).to_be_visible()
        width = page.evaluate("document.documentElement.scrollWidth")
        assert width <= 390
    finally:
        context.close()
