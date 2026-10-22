"""Keyboard shortcuts and accessibility basics across the whole page."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.e2e.test_devices_ui import tile
from tests.e2e.test_ui import Server, sync_api

pytestmark = pytest.mark.e2e
expect = sync_api.expect

# Every visible control needs a name a screen reader can say; every image an alt.
AUDIT = """() => {
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
    && getComputedStyle(el).visibility !== "hidden";
  const text = (el) => (el.textContent || "").replace(/\\s+/g, " ").trim();
  const name = (el) => {
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label");
    const by = el.getAttribute("aria-labelledby");
    if (by) return by.split(" ").map((id) => text(document.getElementById(id) || document.body)).join(" ");
    if (el.labels && el.labels.length) return [...el.labels].map(text).join(" ");
    if (el.matches("input[type=search], input[type=text], textarea") && el.placeholder) return el.placeholder;
    return text(el) || el.getAttribute("title") || el.getAttribute("alt") || "";
  };
  const problems = [];
  for (const el of document.querySelectorAll("button, a[href], input:not([type=hidden]), select, textarea, [role=button], [role=menuitem], [role=switch]")) {
    if (el.closest("[hidden], dialog:not([open])") || !visible(el)) continue;
    if (!name(el).trim()) problems.push(`no name: ${el.outerHTML.slice(0, 120)}`);
  }
  for (const img of document.querySelectorAll("img")) {
    if (!img.hasAttribute("alt")) problems.push(`img without alt: ${img.outerHTML.slice(0, 120)}`);
  }
  for (const dialog of document.querySelectorAll("dialog")) {
    if (!dialog.getAttribute("aria-labelledby") && !dialog.getAttribute("aria-label")) problems.push(`unnamed dialog #${dialog.id}`);
  }
  return problems;
}"""


@pytest.fixture
def full_mac(devices: list[Server]) -> Server:
    alpha = devices[0]
    for i in range(7):  # enough files for the search box
        (alpha.root / f"note {i}.txt").write_text(f"note {i}")
    return alpha


def test_page_passes_the_accessibility_audit(full_mac: Server, page) -> None:  # type: ignore[no-untyped-def]
    page.goto(full_mac.url)
    expect(tile(page, "Charlie")).to_be_visible(timeout=10_000)
    expect(page.locator(".file-row")).to_have_count(7, timeout=10_000)
    assert page.evaluate(AUDIT) == []
    # …and with the send bar, a menu and the preview open.
    tile(page, "Bravo").locator(".device-button").click()
    assert page.evaluate(AUDIT) == []
    page.locator(".file-row").first.locator(".row-more").click()
    assert page.evaluate(AUDIT) == []
    page.keyboard.press("Escape")
    page.locator(".file-row").first.locator(".thumb").click()
    expect(page.locator("#preview-dialog")).to_be_visible()
    assert page.evaluate(AUDIT) == []


def test_live_regions_announce_changes(full_mac: Server, page) -> None:  # type: ignore[no-untyped-def]
    page.goto(full_mac.url)
    expect(page.locator("#main-view")).to_be_visible(timeout=10_000)
    for selector in ("#toasts", "#connection", "#drop-live"):
        assert page.locator(selector).get_attribute("aria-live") in {"polite", "assertive"}, (
            selector
        )


def test_keyboard_shortcuts(
    full_mac: Server, devices: list[Server], page, second_page, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    bravo = devices[1]
    page.goto(full_mac.url)
    second_page.goto(bravo.url)
    expect(tile(page, "Bravo")).to_be_visible(timeout=10_000)
    expect(page.locator(".file-row")).to_have_count(7, timeout=10_000)

    # ? lists the shortcuts; Esc closes the list.
    page.keyboard.press("?")
    dialog = page.locator("#shortcuts-dialog")
    expect(dialog).to_be_visible()
    for key in ("Ctrl", "Enter", "/", "Esc", "Alt", "Shift"):
        expect(dialog).to_contain_text(key)
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()

    # / goes to search.
    page.keyboard.press("/")
    expect(page.locator("#search")).to_be_focused()
    page.keyboard.press("Escape")
    page.evaluate("document.activeElement.blur()")

    # Ctrl/⌘+O chooses files; Ctrl/⌘+Enter sends them to the chosen devices.
    sample = tmp_path / "keyboard.txt"
    sample.write_text("typed, not clicked")
    with page.expect_file_chooser() as chooser:
        page.keyboard.press("ControlOrMeta+o")
    chooser.value.set_files(str(sample))
    tile(page, "Bravo").locator(".device-button").focus()
    page.keyboard.press("Enter")  # select it, from the keyboard
    expect(tile(page, "Bravo").locator(".device-button")).to_have_attribute("aria-pressed", "true")
    page.keyboard.press("ControlOrMeta+Enter")
    second_page.locator("#incoming-accept").click(timeout=10_000)
    expect(page.locator(".job-target", has_text="Bravo")).to_contain_text(
        "Delivered", timeout=10_000
    )

    # Esc with something staged clears it.
    page.evaluate("document.activeElement.blur()")
    page.set_input_files("#file-input", str(sample))
    expect(page.locator("#send-bar")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#send-bar")).to_be_hidden()


def test_focus_is_visible_on_controls(full_mac: Server, page) -> None:  # type: ignore[no-untyped-def]
    page.goto(full_mac.url)
    expect(tile(page, "Bravo")).to_be_visible(timeout=10_000)
    button = tile(page, "Bravo").locator(".device-button")
    page.keyboard.press("Tab")
    button.focus()
    outline = button.evaluate(
        "(el) => getComputedStyle(el).outlineStyle + ' ' + getComputedStyle(el).boxShadow"
    )
    assert re.search(r"solid|auto|rgb", outline), outline
