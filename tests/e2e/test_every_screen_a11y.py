"""Past the first screen: the file list, menus, dialogs and transfer cards are readable,
easy to hit with a thumb, and show where the keyboard is. Measured in a real browser."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.test_first_screen_a11y import LOW_CONTRAST, SMALL_TARGETS
from tests.e2e.test_ui import Server, _start, sync_api

pytestmark = pytest.mark.e2e
expect = sync_api.expect

PHONE = {"viewport": {"width": 375, "height": 740}, "has_touch": True, "is_mobile": True}
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154"
    "789c63fcffff3f0300050001ff5b2dd9c10000000049454e44ae426082"
)  # fmt: skip


def walk(page, other, alpha: Server, bravo: Server) -> Iterator[tuple[str, str]]:  # type: ignore[no-untyped-def]
    """Open each screen in turn and say which part of the page to measure."""
    (alpha.root / "report.pdf").write_bytes(b"%PDF-1.4 " + b"x" * 3000)
    (alpha.root / "photo.png").write_bytes(PNG)
    (bravo.root / "report.pdf").write_bytes(
        b"older copy"
    )  # so the incoming list says "Already here"
    page.goto(alpha.url)
    other.goto(bravo.url)
    device = page.locator(".device", has_text="Bravo")
    device.wait_for()
    page.locator(".file-row").first.wait_for()
    yield "the file list", "body"
    page.locator(".row-more").first.click()
    yield "a file's menu", "#file-menu"
    page.keyboard.press("Escape")
    page.locator(".file-row", has_text="photo.png").locator(".thumb, .row-main").first.click()
    yield "the preview", "#preview-dialog"
    page.keyboard.press("Escape")
    page.set_input_files(
        "#file-input", [str(alpha.root / "report.pdf"), str(alpha.root / "photo.png")]
    )
    device.locator(".device-button").click()
    yield "the send bar", "#send-bar"
    page.click("#send-text")
    yield "the text dialog", "#text-dialog"
    page.keyboard.press("Escape")
    page.click("#send-go")
    other.locator("#incoming-dialog[open]").wait_for()
    yield "an offer to accept", "#incoming-dialog"  # measured on the other device's page
    page.locator("#job-list .job").first.wait_for()
    yield "a transfer card", "#job-list"
    other.locator("#incoming-accept").click()
    page.get_by_role("button", name="Dismiss 2 files").wait_for()
    yield "a finished transfer", "body"
    page.click("#connect-button")
    yield "the add-device sheet", "#connect-dialog"
    page.locator("#connect-tabs [data-tab=enter]").click()
    yield "the enter-a-code tab", "#connect-dialog"


def screens(browser, devices: list[Server], scheme: str, **context):  # type: ignore[no-untyped-def]
    alpha, bravo = devices[0], devices[1]
    mine = browser.new_context(color_scheme=scheme, **context)
    theirs = browser.new_context(color_scheme=scheme, **context)
    steps = walk(mine.new_page(), theirs.new_page(), alpha, bravo)
    for label, sel in steps:
        # the offer is on the other device's page, the rest on the first
        screen = (theirs if label == "an offer to accept" else mine).pages[0]
        screen.evaluate(  # let dialogs finish growing in, or they measure a little small
            "Promise.all(document.getAnimations().filter((a) => a.effect.getComputedTiming().iterations < Infinity).map((a) => a.finished.catch(() => 0)))"
        )
        yield label, sel, screen
    mine.close()
    theirs.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_screen_is_readable(browser, devices: list[Server], scheme: str) -> None:  # type: ignore[no-untyped-def]
    found = {
        label: bad
        for label, sel, page in screens(browser, devices, scheme, **PHONE)
        if (bad := page.evaluate(LOW_CONTRAST, sel))
    }
    assert not found, "\n".join(f"{screen}: {x}" for screen, xs in found.items() for x in xs)


def test_every_screen_is_big_enough_for_a_thumb(browser, devices: list[Server]) -> None:  # type: ignore[no-untyped-def]
    found = {
        label: small
        for label, sel, page in screens(browser, devices, "light", **PHONE)
        if (small := page.evaluate(SMALL_TARGETS, sel))
    }
    assert not found, "\n".join(f"{screen}: {x}" for screen, xs in found.items() for x in xs)


NO_RING = """() => { const e = document.activeElement, s = getComputedStyle(e);
  if (e === document.body || e.tagName === 'DIALOG') return null;  // nothing to see, or the sheet itself
  return s.outlineStyle === 'none' || parseFloat(s.outlineWidth) < 2
    ? `${e.tagName}#${e.id}.${e.className} "${(e.getAttribute('aria-label') || e.textContent).trim().slice(0, 20)}"` : null; }"""


def test_the_keyboard_always_shows_where_it_is(browser, devices: list[Server]) -> None:  # type: ignore[no-untyped-def]
    alpha = devices[0]
    (alpha.root / "report.pdf").write_bytes(b"%PDF-1.4 " + b"x" * 3000)
    page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
    page.goto(alpha.url)
    page.locator(".file-row").first.wait_for()
    missing: list[str] = []

    def tab(times: int, key: str = "Tab") -> None:
        for _ in range(times):
            page.keyboard.press(key)
            if lost := page.evaluate(NO_RING):
                missing.append(lost)

    tab(30)  # the whole first screen, the file list and its filters
    page.locator(".row-more").first.focus()
    page.keyboard.press("Enter")  # opened from the keyboard, as the person would
    tab(3, "ArrowDown")  # the file's menu
    page.keyboard.press("Escape")
    page.locator("#connect-button").focus()
    page.keyboard.press("Enter")
    tab(8)  # inside the add-device sheet, starting on its selected tab
    assert not missing, "\n".join(sorted(set(missing)))


def test_a_toast_never_covers_the_send_bar(browser, devices: list[Server]) -> None:  # type: ignore[no-untyped-def]
    alpha = devices[0]
    (alpha.root / "report.pdf").write_bytes(b"%PDF-1.4 " + b"x" * 3000)
    page = browser.new_context(**PHONE).new_page()
    page.goto(alpha.url)
    page.locator(".device", has_text="Bravo").wait_for()
    page.set_input_files("#file-input", str(alpha.root / "report.pdf"))
    toast = page.locator(".toast", has_text="file ready")
    toast.wait_for()
    page.evaluate("Promise.all(document.getAnimations().map((a) => a.finished.catch(() => 0)))")
    assert (
        toast.bounding_box()["y"] + toast.bounding_box()["height"]
        <= (page.locator(".send-bar-inner").bounding_box()["y"])
    )


@pytest.fixture
def visitor(tmp_path: Path) -> Iterator[Server]:
    yield from _start(tmp_path)  # a shared folder: its visitor uploads from the browser


def test_a_failed_upload_says_why_in_full(browser, visitor: Server) -> None:  # type: ignore[no-untyped-def]
    page = browser.new_context(**PHONE).new_page()
    page.goto(visitor.url)
    page.route("**/api/files", lambda route: route.abort())
    page.set_input_files(
        "#file-input", {"name": "film.bin", "mimeType": "x/y", "buffer": b"x" * 100}
    )
    row = page.locator(".transfer-row[data-state=error] .row-sub")
    row.wait_for()
    cut_off = row.evaluate("(e) => e.scrollWidth > e.clientWidth")
    assert not cut_off, row.inner_text()


def test_the_add_device_sheet_starts_with_just_what_you_need(
    browser, devices: list[Server]
) -> None:  # type: ignore[no-untyped-def]
    page = browser.new_context(**PHONE).new_page()
    page.goto(devices[0].url)
    page.click("#connect-button")
    sheet = page.locator("#connect-dialog")
    expect(page.locator("#qr-image")).to_be_visible()
    expect(page.locator("#share-url")).to_be_visible()
    for rare in ("#enc-only", "#invite-new"):
        expect(page.locator(rare)).not_to_be_visible()
    fits = sheet.evaluate("(e) => e.scrollHeight <= e.clientHeight + 1")
    assert fits, "the first view scrolls on a phone"
    page.get_by_text("More options").click()
    for rare in ("#enc-only", "#invite-new"):
        expect(page.locator(rare)).to_be_visible()
