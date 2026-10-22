"""Browser tests for receiving: what to do with files already here, and system
notifications while the window is in the background."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.test_devices_ui import tile
from tests.e2e.test_ui import Server, sync_api

pytestmark = pytest.mark.e2e
expect = sync_api.expect

# Stand-ins for the Notification API and a hidden window (Playwright's pages
# are always "visible"): notifications are recorded in window.__notes.
FAKE_NOTIFICATIONS = """
(() => {
  const setup = %s;
  window.__notes = [];
  window.__hidden = setup.hidden;
  Object.defineProperty(Document.prototype, "hidden", { configurable: true, get: () => window.__hidden });
  Object.defineProperty(Document.prototype, "visibilityState", {
    configurable: true, get: () => (window.__hidden ? "hidden" : "visible"),
  });
  class FakeNotification {
    constructor(title, options = {}) {
      window.__notes.push({ title, body: options.body || "", tag: options.tag || "" });
    }
    close() {}
  }
  FakeNotification.permission = setup.permission;
  FakeNotification.requestPermission = async () => (FakeNotification.permission = "granted");
  window.Notification = FakeNotification;
})();
"""


def notes(pg) -> list[str]:  # type: ignore[no-untyped-def]
    return [n["title"] for n in pg.evaluate("window.__notes")]


def wait_for_note(pg, text: str, timeout: float = 30_000) -> None:  # type: ignore[no-untyped-def]
    pg.wait_for_function(
        "(text) => window.__notes.some((n) => n.title.includes(text))", arg=text, timeout=timeout
    )


def test_choose_what_happens_to_files_already_here(
    devices: list[Server], page, second_page, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo, _ = devices
    (bravo.root / "report.pdf").write_bytes(b"o" * 12)
    page.goto(alpha.url)
    second_page.goto(bravo.url)
    expect(tile(page, "Bravo")).to_be_visible(timeout=10_000)
    for name, data in (("report.pdf", b"n" * 12), ("new.txt", b"x")):
        (tmp_path / name).write_bytes(data)
    tile(page, "Bravo").locator(".device-button").click()
    page.set_input_files("#file-input", [str(tmp_path / "report.pdf"), str(tmp_path / "new.txt")])
    page.click("#send-go")

    dupes = second_page.locator("#incoming-dupes")
    expect(dupes).to_be_visible(timeout=10_000)
    expect(dupes).to_contain_text("1 of these files is already here")
    expect(second_page.locator("#incoming-files li", has_text="report.pdf")).to_contain_text(
        "Already here"
    )
    expect(second_page.locator("input[name=dupes][value=rename]")).to_be_checked()
    second_page.get_by_label("Skip it").check()
    second_page.click("#incoming-accept")
    expect(page.locator(".job-target", has_text="Bravo")).to_contain_text(
        "Delivered", timeout=10_000
    )
    assert (bravo.root / "report.pdf").read_bytes() == b"o" * 12
    assert (bravo.root / "new.txt").read_bytes() == b"x"
    assert not (bravo.root / "report (1).pdf").exists()


def test_no_choice_when_nothing_is_already_here(
    devices: list[Server], page, second_page, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo, _ = devices
    page.goto(alpha.url)
    second_page.goto(bravo.url)
    expect(tile(page, "Bravo")).to_be_visible(timeout=10_000)
    (tmp_path / "fresh.txt").write_text("hi")
    tile(page, "Bravo").locator(".device-button").click()
    page.set_input_files("#file-input", str(tmp_path / "fresh.txt"))
    page.click("#send-go")
    expect(second_page.locator("#incoming-dialog")).to_be_visible(timeout=10_000)
    expect(second_page.locator("#incoming-dupes")).to_be_hidden()


def test_notifications_while_the_window_is_hidden(
    devices: list[Server], browser, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo, _ = devices
    contexts = []
    pages = []
    for server in (alpha, bravo):
        ctx = browser.new_context()
        ctx.add_init_script(FAKE_NOTIFICATIONS % '{"hidden": true, "permission": "granted"}')
        pg = ctx.new_page()
        pg.goto(server.url)
        contexts.append(ctx)
        pages.append(pg)
    a, b = pages
    try:
        expect(tile(a, "Bravo")).to_be_visible(timeout=20_000)
        (tmp_path / "menu.txt").write_text("soup")
        tile(a, "Bravo").locator(".device-button").click()
        a.set_input_files("#file-input", str(tmp_path / "menu.txt"))
        a.click("#send-go")
        wait_for_note(b, "Alpha wants to send you a file")
        b.locator("#incoming-accept").click(timeout=10_000)
        wait_for_note(a, "Sent to Bravo")
        wait_for_note(b, "Received “menu.txt” from Alpha")
        # Visible windows don't notify (the toast is enough).
        assert all("Sent" not in t for t in notes(b))
    finally:
        for ctx in contexts:
            ctx.close()


def test_turning_on_notifications(devices: list[Server], browser) -> None:  # type: ignore[no-untyped-def]
    alpha = devices[0]
    ctx = browser.new_context()
    ctx.add_init_script(FAKE_NOTIFICATIONS % '{"hidden": false, "permission": "default"}')
    pg = ctx.new_page()
    try:
        pg.goto(alpha.url)
        button = pg.locator("#notify-button")
        expect(button).to_be_visible(timeout=10_000)
        expect(button).to_have_attribute("aria-label", "Turn on notifications")
        button.click()
        expect(button).to_be_hidden()
        expect(pg.locator(".toast", has_text="Notifications are on")).to_be_visible()
    finally:
        ctx.close()
