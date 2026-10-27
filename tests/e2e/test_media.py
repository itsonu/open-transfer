"""Browser tests for previews and media: the preview dialog, thumbnails, the
gallery view and each file's quick actions."""

from __future__ import annotations

import os
import re
import struct
import time
import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.test_devices_ui import tile
from tests.e2e.test_ui import Server, _start, sync_api

pytestmark = pytest.mark.e2e
expect = sync_api.expect


def png(rgb: tuple[int, int, int] = (40, 120, 220), size: int = 24) -> bytes:
    """A real PNG (solid colour), so <img> loads it."""
    row = b"\x00" + bytes(rgb) * size
    raw = zlib.compress(row * size)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


@pytest.fixture
def mac(tmp_path: Path) -> Iterator[Server]:
    """One app with a few received files, newest last written."""
    gen = _start(tmp_path, name="Mac", owner=True)
    server = next(gen)
    files = [
        ("beach.png", png((30, 140, 230)), 300),
        ("notes.txt", b"Gate code 4411\nBring towels", 200),
        ("contract.pdf", b"%PDF-1.4\n%%EOF\n", 100),
        ("aurora.png", png((40, 200, 120)), 50),
        ("clip.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64, 10),
    ]
    now = time.time()
    for name, data, age in files:
        path = server.root / name
        path.write_bytes(data)
        os.utime(path, (now - age, now - age))
    yield server
    for _ in gen:
        pass


def names(pg) -> list[str]:  # type: ignore[no-untyped-def]
    return pg.locator("#file-list .file-row .row-title").all_inner_texts()  # type: ignore[no-any-return]


# ----------------------------------------------------------------- preview


def test_preview_steps_through_files_and_closes(mac: Server, page) -> None:  # type: ignore[no-untyped-def]
    page.add_init_script(
        "Object.defineProperty(navigator, 'pdfViewerEnabled', { get: () => true })"
    )
    page.goto(mac.url)
    row = page.locator(".file-row", has_text="beach.png")
    expect(row).to_be_visible(timeout=10_000)
    row.locator(".thumb").click()
    dialog = page.locator("#preview-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#preview-title")).to_have_text("beach.png")
    expect(dialog.locator("img")).to_have_attribute("src", re.compile(r"beach\.png"))
    # Newest first: aurora, clip, contract, notes, beach. → wraps to aurora.
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#preview-title")).to_have_text("notes.txt")
    expect(dialog.locator("pre")).to_contain_text("Gate code 4411")
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#preview-title")).to_have_text("contract.pdf")
    expect(dialog.locator("iframe")).to_have_attribute(
        "src", re.compile(r"contract\.pdf\?inline=1")
    )
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    expect(page.locator("#preview-title")).to_have_text("beach.png")
    expect(page.locator("#preview-count")).to_have_text("5 of 5")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()


def test_no_pdf_viewer_means_no_pdf_preview(mac: Server, page) -> None:  # type: ignore[no-untyped-def]
    """Android's WebView can't show a PDF: framing one hands it to another app."""
    page.add_init_script(
        "Object.defineProperty(navigator, 'pdfViewerEnabled', { get: () => false })"
    )
    page.goto(mac.url)
    row = page.locator(".file-row", has_text="contract.pdf")
    expect(row).to_be_visible(timeout=10_000)
    row.locator(".row-more").click()
    expect(page.locator("#file-menu [role=menuitem]", has_text="Save")).to_be_visible()
    expect(page.locator("#file-menu [role=menuitem]", has_text="Preview")).to_have_count(0)
    page.keyboard.press("Escape")
    # Stepping onto it from another file's preview shows Save, not a frame.
    page.locator(".file-row", has_text="aurora.png").locator(".thumb").click()
    title = page.locator("#preview-title")
    for _ in range(5):
        if title.inner_text() == "contract.pdf":
            break
        page.keyboard.press("ArrowRight")
    expect(title).to_have_text("contract.pdf")
    expect(page.locator("#preview-dialog .preview-none")).to_be_visible()
    assert page.locator("#preview-dialog iframe").count() == 0


def test_preview_keeps_text_as_text(mac: Server, page) -> None:  # type: ignore[no-untyped-def]
    (mac.root / "page.html").write_text("<img src=x onerror=alert(1)><b>bold</b>")
    page.goto(mac.url)
    row = page.locator(".file-row", has_text="page.html")
    expect(row).to_be_visible(timeout=10_000)
    row.locator(".row-more").click()
    page.locator("#file-menu [role=menuitem]", has_text="Preview").click()
    pre = page.locator("#preview-dialog pre")
    expect(pre).to_have_text("<img src=x onerror=alert(1)><b>bold</b>")
    assert page.locator("#preview-dialog b").count() == 0


# --------------------------------------------------------------- thumbnails


def test_videos_get_a_frame_thumbnail(mac: Server, page) -> None:  # type: ignore[no-untyped-def]
    page.goto(mac.url)
    row = page.locator(".file-row", has_text="clip.mp4")
    expect(row).to_be_visible(timeout=10_000)
    video = row.locator(".thumb video")
    expect(video).to_have_count(1)
    expect(video).to_have_attribute("preload", "metadata")


def test_staged_and_sent_images_show_thumbnails(
    devices: list[Server], page, second_page, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo, _ = devices
    page.goto(alpha.url)
    second_page.goto(bravo.url)
    expect(tile(page, "Bravo")).to_be_visible(timeout=10_000)
    pic = tmp_path / "sunset.png"
    pic.write_bytes(png((230, 120, 40)))
    page.set_input_files("#file-input", str(pic))
    thumb = page.locator("#send-bar-thumbs img")
    expect(thumb).to_have_count(1)
    expect(thumb).to_have_attribute("src", re.compile(r"^blob:"))
    tile(page, "Bravo").locator(".device-button").click()
    page.click("#send-go")
    expect(page.locator(".job", has_text="sunset.png").locator(".thumb img")).to_have_count(1)
    second_page.locator("#incoming-accept").click(timeout=10_000)
    history = page.locator(".history-row", has_text="sunset.png")
    expect(history.locator(".thumb img")).to_have_attribute(
        "src", re.compile(r"^data:image/"), timeout=10_000
    )


# ------------------------------------------------------------------ gallery


def test_gallery_view_filters_sorts_and_remembers(mac: Server, page) -> None:  # type: ignore[no-untyped-def]
    page.goto(mac.url)
    expect(page.locator(".file-row")).to_have_count(5, timeout=10_000)
    assert names(page)[0] == "clip.mp4"  # newest first
    page.click("#view-grid")
    expect(page.locator("#view-grid")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#file-list")).to_have_class(re.compile(r"\bis-grid\b"))
    page.select_option("#library-filter", "image")
    expect(page.locator(".file-row")).to_have_count(2)
    page.select_option("#library-sort", "name")
    assert names(page) == ["aurora.png", "beach.png"]
    page.select_option("#library-filter", "document")
    assert names(page) == ["contract.pdf", "notes.txt"]
    page.reload()
    expect(page.locator(".file-row")).to_have_count(2, timeout=10_000)
    expect(page.locator("#file-list")).to_have_class(re.compile(r"\bis-grid\b"))
    expect(page.locator("#library-filter")).to_have_value("document")
    expect(page.locator("#library-sort")).to_have_value("name")
    page.select_option("#library-filter", "all")
    page.select_option("#library-sort", "size")
    assert names(page)[-1] in {"contract.pdf", "notes.txt"}  # smallest last
    # In the grid, a click opens the full-screen preview.
    page.locator(".file-row", has_text="beach.png").locator(".thumb").click()
    expect(page.locator("#preview-dialog")).to_be_visible()


# ------------------------------------------------------------ quick actions


def test_quick_actions_menu_by_mouse_and_keyboard(mac: Server, browser) -> None:  # type: ignore[no-untyped-def]
    context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    page = context.new_page()
    try:
        page.goto(mac.url)
        row = page.locator(".file-row", has_text="notes.txt")
        expect(row).to_be_visible(timeout=10_000)
        more = row.locator(".row-more")
        expect(more).to_have_attribute("aria-label", "More actions for notes.txt")
        more.click()
        menu = page.locator("#file-menu")
        expect(menu).to_be_visible()
        items = menu.locator("[role=menuitem]").all_inner_texts()
        for wanted in ("Preview", "Copy text", "Copy link", "Save", "Delete"):
            assert wanted in items, items
        menu.locator("[role=menuitem]", has_text="Copy text").click()
        expect(menu).to_be_hidden()
        expect(page.locator(".toast", has_text="Copied to clipboard")).to_be_visible()
        assert page.evaluate("navigator.clipboard.readText()") == "Gate code 4411\nBring towels"

        # Keyboard: Enter opens with focus on the first item, arrows move, Esc returns focus.
        more.focus()
        page.keyboard.press("Enter")
        expect(menu.locator("[role=menuitem]").first).to_be_focused()
        page.keyboard.press("ArrowDown")
        expect(menu.locator("[role=menuitem]").nth(1)).to_be_focused()
        page.keyboard.press("Escape")
        expect(menu).to_be_hidden()
        expect(more).to_be_focused()

        # Right-click a row opens the same menu; "Copy link" copies the file's address.
        page.locator(".file-row", has_text="beach.png").click(button="right")
        expect(menu).to_be_visible()
        assert "Copy image" in menu.locator("[role=menuitem]").all_inner_texts()
        menu.locator("[role=menuitem]", has_text="Copy link").click()
        expect(page.locator(".toast", has_text="Link copied")).to_be_visible()
        link = page.evaluate("navigator.clipboard.readText()")
        assert link.endswith("/files/beach.png"), link
    finally:
        context.close()


# ------------------------------------------------------------------- layout


def test_compact_drop_zone_keeps_its_text_in_one_column(mac: Server, page) -> None:  # type: ignore[no-untyped-def]
    """With files listed the drop zone is one row: icon, then title, link, clipboard and hint."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(mac.url)
    zone = page.locator("#dropzone")
    expect(zone).to_have_class(re.compile(r"\bis-compact\b"), timeout=10_000)
    expect(page.locator("#dropzone-clipboard")).to_be_visible()
    page.evaluate("document.querySelector('#drop-hint').textContent = 'Then choose who gets them.'")
    icon = page.locator(".drop-icon").bounding_box()
    lefts = {
        sel: page.locator(sel).bounding_box()["x"]  # type: ignore[index]
        for sel in ("#drop-title", ".drop-sub", "#dropzone-clipboard", "#drop-hint")
    }
    assert icon is not None
    for sel, x in lefts.items():
        assert icon["x"] + icon["width"] <= x <= icon["x"] + icon["width"] + 24, (sel, icon, x)
    # The clipboard button hugs its label instead of stretching and centring it.
    glyph = page.locator("#dropzone-clipboard .icon").bounding_box()
    assert glyph is not None
    assert glyph["x"] - lefts["#drop-title"] <= 24, (glyph, lefts["#drop-title"])
