"""Browser tests for the clipboard: paste to send, send the clipboard, copy
shortcuts, privacy checks, and syncing between paired apps."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.test_ui import Server, _start, sync_api
from tests.test_clipboard import SENSITIVE_CASES

pytestmark = pytest.mark.e2e
expect = sync_api.expect
CLIPBOARD = ["clipboard-read", "clipboard-write"]


def call(server: Server, method: str, path: str, body: object = None) -> dict:  # type: ignore[type-arg]
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        server.url + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as res:
        return json.loads(res.read() or b"{}")  # type: ignore[no-any-return]


@pytest.fixture
def mac(tmp_path: Path) -> Iterator[Server]:
    gen = _start(tmp_path, name="Mac", owner=True)
    server = next(gen)
    (server.root / "notes.txt").write_text("Gate code is on the fridge")
    (server.root / "contract.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    yield server
    for _ in gen:
        pass


@pytest.fixture
def clip_page(browser):  # type: ignore[no-untyped-def]
    context = browser.new_context(permissions=CLIPBOARD)
    pg = context.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    yield pg
    context.close()
    assert errors == [], f"browser errors: {errors}"


def paste_text(pg, text: str) -> None:  # type: ignore[no-untyped-def]
    pg.evaluate(
        """(text) => {
            const dt = new DataTransfer();
            dt.setData("text/plain", text);
            document.body.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true }));
        }""",
        text,
    )


# --------------------------------------------------------------- privacy


def test_browser_and_app_agree_on_sensitive_text(mac: Server, page) -> None:  # type: ignore[no-untyped-def]
    page.goto(mac.url)
    expect(page.locator("#main-view")).to_be_visible(timeout=10_000)
    texts = [text for text, _ in SENSITIVE_CASES]
    got = page.evaluate(
        "async (texts) => { const m = await import('/static/lib.js'); return texts.map(m.looksSensitive); }",
        texts,
    )
    assert got == [reason for _, reason in SENSITIVE_CASES]


# ------------------------------------------------------------------ paste


def test_pasting_text_stages_it_to_send(mac: Server, page) -> None:  # type: ignore[no-untyped-def]
    page.goto(mac.url)
    expect(page.locator("#main-view")).to_be_visible(timeout=10_000)
    paste_text(page, "Dinner at 8, table for four")
    expect(page.locator("#send-bar-files")).to_contain_text("Pasted text")
    expect(page.locator("#send-bar-files")).to_contain_text(".txt")


def test_pasting_something_private_asks_first(mac: Server, page) -> None:  # type: ignore[no-untyped-def]
    page.goto(mac.url)
    expect(page.locator("#main-view")).to_be_visible(timeout=10_000)
    paste_text(page, "4111 1111 1111 1111")
    expect(page.locator("#confirm-title")).to_have_text("This looks like a card number")
    page.locator("#confirm-dialog button[value=cancel]").click()
    expect(page.locator("#send-bar")).to_be_hidden()
    paste_text(page, "4111 1111 1111 1111")
    page.click("#confirm-ok")
    expect(page.locator("#send-bar-files")).to_contain_text("Pasted text")


# ------------------------------------------------------------------- copy


def test_copy_shortcut_on_a_file(mac: Server, clip_page) -> None:  # type: ignore[no-untyped-def]
    page = clip_page
    page.goto(mac.url)
    notes = page.locator(".file-row", has_text="notes.txt")
    expect(notes).to_be_visible(timeout=10_000)
    notes.locator(".row-title").focus()
    page.keyboard.press("ControlOrMeta+c")
    expect(page.locator(".toast", has_text="Copied to clipboard")).to_be_visible()
    assert page.evaluate("navigator.clipboard.readText()") == "Gate code is on the fridge"
    # Files that aren't text or pictures: their link.
    page.locator(".file-row", has_text="contract.pdf").locator(".row-title").focus()
    page.keyboard.press("ControlOrMeta+c")
    expect(page.locator(".toast", has_text="Link copied")).to_be_visible()
    assert page.evaluate("navigator.clipboard.readText()").endswith("/files/contract.pdf")


def test_send_clipboard_stages_its_text(mac: Server, clip_page) -> None:  # type: ignore[no-untyped-def]
    page = clip_page
    page.goto(mac.url)
    expect(page.locator("#main-view")).to_be_visible(timeout=10_000)
    page.evaluate("navigator.clipboard.writeText('Flight AB123, gate 14')")
    page.keyboard.press("ControlOrMeta+Shift+v")
    expect(page.locator("#send-bar-files")).to_contain_text("Clipboard")
    page.click("#send-clear")
    expect(page.locator("#send-bar")).to_be_hidden()
    page.evaluate("navigator.clipboard.writeText('Second thing')")
    page.locator("#dropzone-clipboard").click()
    expect(page.locator("#send-bar-files")).to_contain_text("Clipboard")


# ------------------------------------------------------------------- sync


@pytest.fixture
def pair_of_apps(tmp_path: Path) -> Iterator[tuple[Server, Server]]:
    gens = []
    first = _start(tmp_path, name="Alpha", owner=True)
    alpha = next(first)
    gens.append(first)
    second = _start(tmp_path, name="Bravo", owner=True)
    bravo = next(second)
    gens.append(second)
    code = call(bravo, "POST", "/api/pair/open")["code"]
    port = bravo.url.rsplit(":", 1)[1]
    call(alpha, "POST", "/api/pair", {"code": code, "address": f"127.0.0.1:{port}"})
    yield alpha, bravo
    for gen in gens:
        for _ in gen:
            pass


def test_clipboard_syncs_between_paired_apps(pair_of_apps: tuple[Server, Server], browser) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo = pair_of_apps
    a_ctx = browser.new_context(permissions=CLIPBOARD)
    b_ctx = browser.new_context(permissions=CLIPBOARD)
    a, b = a_ctx.new_page(), b_ctx.new_page()
    try:
        a.goto(alpha.url)
        b.goto(bravo.url)
        for pg, other in ((a, "Bravo"), (b, "Alpha")):
            section = pg.locator("#clipboard-section")
            expect(section).to_be_visible(timeout=10_000)
            pg.click("#clip-settings")
            toggle = pg.locator("#clip-devices label", has_text=other).locator("input")
            expect(toggle).not_to_be_checked()
            toggle.check()
            expect(pg.locator("#clip-sync-note")).to_contain_text(other, timeout=5000)
            pg.locator("#clip-dialog .sheet-close").click()

        a.bring_to_front()
        a.evaluate("navigator.clipboard.writeText('Door code 2580#')")
        a.click("#clip-push")
        expect(a.locator(".toast", has_text="Shared with Bravo")).to_be_visible(timeout=10_000)
        expect(a.locator("#clip-list .clip-row")).to_have_count(1)

        received = b.locator("#clip-list .clip-row", has_text="Door code 2580#")
        expect(received).to_be_visible(timeout=10_000)
        expect(received).to_contain_text("From Alpha")
        expect(b.locator(".toast", has_text="Clipboard from Alpha")).to_be_visible()

        # Search, copy again, clear.
        b.fill("#clip-search", "nothing like it")
        expect(b.locator("#clip-list .clip-row")).to_have_count(0)
        b.fill("#clip-search", "door")
        expect(b.locator("#clip-list .clip-row")).to_have_count(1)
        b.bring_to_front()
        received.locator("button", has_text="Copy").click()
        expect(b.locator(".toast").get_by_text("Copied", exact=True)).to_be_visible()
        assert b.evaluate("navigator.clipboard.readText()") == "Door code 2580#"
        b.click("#clip-clear")
        b.click("#confirm-ok")
        expect(b.locator("#clip-list .clip-row")).to_have_count(0)
    finally:
        a_ctx.close()
        b_ctx.close()


def test_private_clips_ask_first_and_are_not_kept(
    pair_of_apps: tuple[Server, Server], browser
) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo = pair_of_apps
    bravo_id = next(d["id"] for d in call(alpha, "GET", "/api/clipboard")["devices"])
    alpha_id = next(d["id"] for d in call(bravo, "GET", "/api/clipboard")["devices"])
    call(alpha, "PUT", "/api/clipboard/settings", {"allowed": [bravo_id]})
    call(bravo, "PUT", "/api/clipboard/settings", {"allowed": [alpha_id]})
    ctx = browser.new_context(permissions=CLIPBOARD)
    a = ctx.new_page()
    try:
        a.goto(alpha.url)
        expect(a.locator("#clipboard-section")).to_be_visible(timeout=10_000)
        a.evaluate("navigator.clipboard.writeText('Tr0ub4dor&3xQ!')")
        a.click("#clip-push")
        expect(a.locator("#confirm-title")).to_have_text("This looks like a password")
        a.click("#confirm-ok")
        expect(a.locator(".toast", has_text="Shared with Bravo")).to_be_visible(timeout=10_000)
        row = a.locator("#clip-list .clip-row")
        expect(row).to_contain_text("Not kept")
    finally:
        ctx.close()


def test_auto_share_sends_what_you_copy(pair_of_apps: tuple[Server, Server], browser) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo = pair_of_apps
    bravo_id = next(d["id"] for d in call(alpha, "GET", "/api/clipboard")["devices"])
    alpha_id = next(d["id"] for d in call(bravo, "GET", "/api/clipboard")["devices"])
    call(alpha, "PUT", "/api/clipboard/settings", {"allowed": [bravo_id]})
    call(bravo, "PUT", "/api/clipboard/settings", {"allowed": [alpha_id]})
    ctx = browser.new_context(permissions=CLIPBOARD)
    a = ctx.new_page()
    try:
        a.goto(alpha.url)
        expect(a.locator("#clip-sync-note")).to_contain_text("Bravo", timeout=10_000)
        a.evaluate("navigator.clipboard.writeText('already there')")
        a.check("#clip-auto")
        a.wait_for_timeout(500)  # what was already copied isn't shared
        a.evaluate("navigator.clipboard.writeText('Copied just now')")

        def received() -> list[str]:
            return [i.get("text") for i in call(bravo, "GET", "/api/clipboard")["items"]]

        for _ in range(50):
            if "Copied just now" in received():
                break
            a.wait_for_timeout(200)
        assert received() == ["Copied just now"]
        # Private-looking text is never shared automatically.
        a.evaluate("navigator.clipboard.writeText('Tr0ub4dor&3xQ!')")
        expect(a.locator(".toast", has_text="looks private")).to_be_visible(timeout=5000)
        assert received() == ["Copied just now"]
    finally:
        ctx.close()
