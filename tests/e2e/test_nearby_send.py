"""Browser tests for sending faster: drop targets, several recipients at once,
favorites, quick send and the device grid on phones, tablets and desktops.

Same setup as ``test_devices_ui``: Alpha, plus Bravo and Charlie connected to it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.e2e.test_devices_ui import tile
from tests.e2e.test_ui import Server, sync_api

pytestmark = pytest.mark.e2e
expect = sync_api.expect


def files_handle(pg, name: str = "dropped.txt", body: str = "dropped bytes"):  # type: ignore[no-untyped-def]
    return pg.evaluate_handle(
        """([name, body]) => {
            const dt = new DataTransfer();
            dt.items.add(new File([body], name, { type: "text/plain" }));
            return dt;
        }""",
        [name, body],
    )


def start_drag(pg, handle) -> None:  # type: ignore[no-untyped-def]
    pg.dispatch_event("body", "dragenter", {"dataTransfer": handle})


def drop_on(locator, handle) -> None:  # type: ignore[no-untyped-def]
    locator.dispatch_event("dragover", {"dataTransfer": handle})
    locator.dispatch_event("drop", {"dataTransfer": handle})


def grid_names(pg) -> list[str]:  # type: ignore[no-untyped-def]
    return pg.locator("#device-grid .device .device-name").all_inner_texts()  # type: ignore[no-any-return]


# ----------------------------------------------------------- drop targets


def test_dragging_shows_where_files_will_go(devices: list[Server], page) -> None:  # type: ignore[no-untyped-def]
    alpha = devices[0]
    page.goto(alpha.url)
    bravo = tile(page, "Bravo")
    expect(bravo).to_be_visible(timeout=10_000)
    handle = files_handle(page)
    start_drag(page, handle)
    expect(bravo.locator(".device-drop-hint")).to_be_visible()
    expect(bravo.locator(".device-drop-hint")).to_have_text("Drop to send")
    bravo.dispatch_event("dragover", {"dataTransfer": handle})
    expect(bravo).to_have_class(re.compile(r"\bis-drop-target\b"))
    expect(page.locator("#drop-live")).to_have_text("Release to send to Bravo")
    bravo.dispatch_event("dragleave", {"dataTransfer": handle})
    expect(bravo).not_to_have_class(re.compile(r"\bis-drop-target\b"))


def test_dropping_on_an_offline_device_says_why(devices: list[Server], page) -> None:  # type: ignore[no-untyped-def]
    alpha = devices[0]
    page.goto(alpha.url)
    ghost = {"id": "ghost-pc", "name": "Ghost PC", "form": "computer", "platform": "windows"}
    page.evaluate("(f) => localStorage.setItem('ot-favorites', JSON.stringify([f]))", ghost)
    page.reload()
    offline = tile(page, "Ghost PC")
    expect(offline).to_have_class(re.compile(r"\bis-offline\b"), timeout=10_000)
    handle = files_handle(page)
    start_drag(page, handle)
    expect(offline.locator(".device-drop-hint")).to_be_hidden()
    drop_on(offline, handle)
    expect(page.locator(".toast", has_text="Ghost PC isn’t nearby")).to_be_visible()
    expect(page.locator("#send-bar")).to_be_hidden()  # nothing was staged either


# ------------------------------------------------------ several recipients


def test_dropping_on_a_selected_device_sends_to_every_selected_one(
    devices: list[Server], page, second_page, third_page
) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo, charlie = devices
    page.goto(alpha.url)
    second_page.goto(bravo.url)
    third_page.goto(charlie.url)
    expect(tile(page, "Charlie")).to_be_visible(timeout=10_000)
    tile(page, "Bravo").locator(".device-button").click()
    tile(page, "Charlie").locator(".device-button").click()
    handle = files_handle(page, "both.txt", "for both")
    start_drag(page, handle)
    expect(tile(page, "Bravo").locator(".device-drop-hint")).to_have_text("Drop to send to 2")
    drop_on(tile(page, "Bravo"), handle)
    # Both selected devices are everyone nearby, so it asks first (like Send).
    expect(page.locator("#confirm-title")).to_have_text("Send to everyone nearby?")
    page.click("#confirm-ok")
    second_page.locator("#incoming-accept").click(timeout=10_000)
    third_page.locator("#incoming-accept").click(timeout=10_000)
    for name in ("Bravo", "Charlie"):
        expect(page.locator(".job-target", has_text=name)).to_contain_text(
            "Delivered", timeout=10_000
        )
    assert (bravo.root / "both.txt").read_text() == "for both"
    assert (charlie.root / "both.txt").read_text() == "for both"


def test_everyone_target_sends_to_all_nearby_devices(
    devices: list[Server], page, second_page, third_page
) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo, charlie = devices
    page.goto(alpha.url)
    second_page.goto(bravo.url)
    third_page.goto(charlie.url)
    expect(tile(page, "Charlie")).to_be_visible(timeout=10_000)
    everyone = page.locator("#everyone-target")
    expect(everyone).to_be_hidden()
    handle = files_handle(page, "all.txt", "for everyone")
    start_drag(page, handle)
    expect(everyone).to_be_visible()
    expect(everyone).to_contain_text("Everyone")
    drop_on(everyone, handle)
    expect(page.locator("#confirm-title")).to_have_text("Send to everyone nearby?")
    page.click("#confirm-ok")
    second_page.locator("#incoming-accept").click(timeout=10_000)
    third_page.locator("#incoming-accept").click(timeout=10_000)
    expect(page.locator(".job-target", has_text="Charlie")).to_contain_text(
        "Delivered", timeout=10_000
    )
    assert (bravo.root / "all.txt").read_text() == "for everyone"
    assert (charlie.root / "all.txt").read_text() == "for everyone"


# ------------------------------------------------------ favorites & quick send


def test_favorite_devices_come_first_and_are_remembered(devices: list[Server], page) -> None:  # type: ignore[no-untyped-def]
    alpha = devices[0]
    page.goto(alpha.url)
    expect(tile(page, "Charlie")).to_be_visible(timeout=10_000)
    assert grid_names(page) == ["Bravo", "Charlie"]
    star = tile(page, "Charlie").locator(".device-fav")
    expect(star).to_have_attribute("aria-pressed", "false")
    expect(star).to_have_attribute("aria-label", "Add Charlie to favorites")
    star.click()
    expect(star).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#device-grid .device").first).to_contain_text("Charlie")
    page.reload()
    expect(tile(page, "Charlie")).to_be_visible(timeout=10_000)
    assert grid_names(page) == ["Charlie", "Bravo"]
    saved = json.loads(page.evaluate("localStorage.getItem('ot-favorites')"))
    assert [f["name"] for f in saved] == ["Charlie"]
    tile(page, "Charlie").locator(".device-fav").click()
    expect(page.locator("#device-grid .device").first).to_contain_text("Bravo")


def test_quick_send_to_a_favorite_skips_the_send_bar(
    devices: list[Server], page, second_page, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo, _ = devices
    page.goto(alpha.url)
    second_page.goto(bravo.url)
    expect(tile(page, "Bravo")).to_be_visible(timeout=10_000)
    expect(tile(page, "Bravo").locator(".device-quick")).to_be_hidden()  # favorites only
    tile(page, "Bravo").locator(".device-fav").click()
    quick = tile(page, "Bravo").locator(".device-quick")
    expect(quick).to_have_attribute("aria-label", "Quick send to Bravo")
    sample = tmp_path / "quick.txt"
    sample.write_text("fast")
    with page.expect_file_chooser() as chooser:
        quick.click()
    chooser.value.set_files(str(sample))
    second_page.locator("#incoming-accept").click(timeout=10_000)
    expect(page.locator(".job-target", has_text="Bravo")).to_contain_text(
        "Delivered", timeout=10_000
    )
    assert (bravo.root / "quick.txt").read_text() == "fast"

    # Alt+1 quick-sends to the first favorite.
    again = tmp_path / "again.txt"
    again.write_text("faster")
    page.evaluate("document.activeElement.blur()")  # (a click could hit the home link)
    with page.expect_file_chooser() as chooser:
        page.keyboard.press("Alt+1")
    chooser.value.set_files(str(again))
    second_page.locator("#incoming-accept").click(timeout=10_000)
    again_row = page.locator(".job", has_text="again.txt")
    expect(again_row.locator(".job-target", has_text="Bravo")).to_contain_text(
        "Delivered", timeout=10_000
    )
    assert (bravo.root / "again.txt").read_text() == "faster"


# ------------------------------------------------------------------ layout


@pytest.mark.parametrize(
    ("width", "height", "touch"),
    [(390, 844, True), (800, 1280, True), (1280, 800, False)],
    ids=["phone", "tablet", "desktop"],
)
def test_device_grid_fits_phones_tablets_and_desktops(
    devices: list[Server], browser, width: int, height: int, touch: bool, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    alpha = devices[0]
    context = browser.new_context(
        viewport={"width": width, "height": height}, has_touch=touch, is_mobile=touch
    )
    pg = context.new_page()
    try:
        pg.goto(alpha.url)
        expect(tile(pg, "Charlie")).to_be_visible(timeout=10_000)
        overflow = pg.evaluate("document.scrollingElement.scrollWidth - innerWidth")
        assert overflow <= 0, f"horizontal scroll of {overflow}px"
        for button in pg.locator("#device-grid .device-button").all():
            box = button.bounding_box()
            assert box, button
            assert box["width"] >= 96, box
            assert box["height"] >= 44, box
        star = tile(pg, "Bravo").locator(".device-fav")
        if touch:  # no hover on touch screens: the star is always there
            expect(star).to_be_visible()
            box = star.bounding_box()
            assert box, star
            assert min(box["width"], box["height"]) >= 32, box
        # The send bar never covers the last thing on the page.
        sample = tmp_path / "staged.txt"
        sample.write_text("x")
        pg.set_input_files("#file-input", str(sample))
        expect(pg.locator("#send-bar")).to_be_visible()
        pg.evaluate("window.scrollTo(0, document.scrollingElement.scrollHeight)")
        bar_top = pg.locator("#send-bar").bounding_box()["y"]
        last = pg.locator("main .view:not([hidden]) > section:not([hidden])").last.bounding_box()
        assert last["y"] + last["height"] <= bar_top + 1, (last, bar_top)
    finally:
        context.close()


def test_device_controls_have_names(devices: list[Server], page) -> None:  # type: ignore[no-untyped-def]
    alpha = devices[0]
    page.goto(alpha.url)
    expect(tile(page, "Charlie")).to_be_visible(timeout=10_000)
    unnamed = page.evaluate(
        """() => [...document.querySelectorAll('#nearby button')]
            .filter((b) => !(b.getAttribute('aria-label') || b.textContent.trim()))
            .map((b) => b.outerHTML.slice(0, 80))"""
    )
    assert unnamed == []
