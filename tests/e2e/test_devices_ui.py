"""Browser tests for nearby devices: choosing recipients, accepting, pairing.

Each "device" is a real ``open-transfer`` process; their owners' windows are
pages on 127.0.0.1. Devices are linked with ``--peer`` so the tests don't
depend on multicast.
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from open_transfer import network
from tests.e2e.test_ui import Server, _start, sync_api

pytestmark = pytest.mark.e2e
expect = sync_api.expect


def _api(server: Server, path: str) -> dict:  # type: ignore[type-arg]
    with urllib.request.urlopen(f"{server.url}{path}", timeout=5) as res:
        return json.loads(res.read())  # type: ignore[no-any-return]


@pytest.fixture
def devices(tmp_path: Path) -> Iterator[list[Server]]:
    """Three apps: Alpha, then Bravo and Charlie which connect to Alpha."""
    started: list[Iterator[Server]] = []
    servers: list[Server] = []
    gen = _start(tmp_path, name="Alpha", owner=True, host="0.0.0.0")
    started.append(gen)
    alpha = next(gen)
    servers.append(alpha)
    for name in ("Bravo", "Charlie"):
        gen = _start(
            tmp_path, "--peer", f"127.0.0.1:{alpha.url.rsplit(':', 1)[1]}", name=name, owner=True
        )
        started.append(gen)
        servers.append(next(gen))
    yield servers
    for gen in started:
        for _ in gen:  # run each generator's cleanup
            pass


@pytest.fixture
def second_page(browser):  # type: ignore[no-untyped-def]
    context = browser.new_context()
    pg = context.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    yield pg
    context.close()
    assert errors == [], f"browser errors: {errors}"


def tile(pg, name: str):  # type: ignore[no-untyped-def]
    return pg.locator(".device", has_text=name)


def test_send_to_a_chosen_device_and_accept(
    devices: list[Server], page, second_page, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo, _ = devices
    page.goto(alpha.url)
    second_page.goto(bravo.url)
    expect(page.locator("#me-name-text")).to_have_text("Alpha")
    expect(tile(page, "Bravo")).to_be_visible(timeout=10_000)

    sample = tmp_path / "Holiday photo.jpg"
    sample.write_bytes(b"x" * 300_000)
    tile(page, "Bravo").locator("button").click()
    expect(tile(page, "Bravo")).to_have_class(re.compile(r"\bis-selected\b"))
    expect(page.locator("#send-bar-to")).to_have_text("To Bravo")
    expect(page.locator("#send-go")).to_be_disabled()
    page.set_input_files("#file-input", str(sample))
    expect(page.locator("#send-bar-files")).to_contain_text("Holiday photo.jpg")
    page.click("#send-go")

    expect(tile(page, "Bravo").locator(".device-sub")).to_have_text("Waiting…")
    dialog = second_page.locator("#incoming-dialog")
    expect(dialog).to_be_visible(timeout=10_000)
    expect(second_page.locator("#incoming-title")).to_have_text("Alpha wants to send you a file")
    expect(second_page.locator("#incoming-files")).to_contain_text("Holiday photo.jpg")
    second_page.click("#incoming-accept")

    expect(page.locator(".job-target", has_text="Bravo")).to_contain_text(
        "Delivered", timeout=10_000
    )
    assert (bravo.root / "Holiday photo.jpg").read_bytes() == sample.read_bytes()
    expect(second_page.locator(".file-row", has_text="Holiday photo.jpg")).to_be_visible(
        timeout=8000
    )
    assert not (devices[2].root / "Holiday photo.jpg").exists()  # Charlie wasn't chosen


def test_declining_tells_the_sender(
    devices: list[Server], page, second_page, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo, _ = devices
    page.goto(alpha.url)
    second_page.goto(bravo.url)
    sample = tmp_path / "nope.txt"
    sample.write_bytes(b"nope")
    tile(page, "Bravo").locator("button").click()
    page.set_input_files("#file-input", str(sample))
    page.click("#send-go")
    second_page.locator("#incoming-decline").click(timeout=10_000)
    expect(page.locator(".toast", has_text="Bravo declined")).to_be_visible(timeout=10_000)
    assert not (bravo.root / "nope.txt").exists()


def test_drop_files_onto_a_device(devices: list[Server], page, second_page) -> None:  # type: ignore[no-untyped-def]
    alpha, _, charlie = devices
    page.goto(alpha.url)
    second_page.goto(charlie.url)
    target = tile(page, "Charlie")
    expect(target).to_be_visible(timeout=10_000)
    handle = page.evaluate_handle(
        """() => {
            const dt = new DataTransfer();
            dt.items.add(new File(["dropped bytes"], "dropped.txt", { type: "text/plain" }));
            return dt;
        }"""
    )
    target.dispatch_event("dragover", {"dataTransfer": handle})
    target.dispatch_event("drop", {"dataTransfer": handle})
    second_page.locator("#incoming-accept").click(timeout=10_000)
    expect(page.locator(".job-target", has_text="Charlie")).to_contain_text(
        "Delivered", timeout=10_000
    )
    assert (charlie.root / "dropped.txt").read_text() == "dropped bytes"


def test_sending_to_everyone_asks_first(devices: list[Server], page, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo, charlie = devices
    page.goto(alpha.url)
    expect(tile(page, "Bravo")).to_be_visible(timeout=10_000)
    expect(tile(page, "Charlie")).to_be_visible(timeout=10_000)
    page.click("#select-all")
    sample = tmp_path / "all.txt"
    sample.write_bytes(b"everyone")
    page.set_input_files("#file-input", str(sample))
    expect(page.locator("#send-go")).to_have_text("Send to 2")
    page.click("#send-go")
    dialog = page.locator("#confirm-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#confirm-title")).to_have_text("Send to everyone nearby?")
    expect(page.locator("#confirm-items")).to_contain_text("Bravo")
    expect(page.locator("#confirm-items")).to_contain_text("Charlie")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    page.wait_for_timeout(500)
    assert _api(bravo, "/api/state")["incoming"] == []
    assert _api(charlie, "/api/state")["incoming"] == []
    # Confirming offers it to both.
    page.click("#send-go")
    page.click("#confirm-ok")
    expect(page.locator(".job-target")).to_have_count(2)


def test_pair_with_a_code(devices: list[Server], page, second_page) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo, _ = devices
    second_page.goto(bravo.url)
    second_page.click("#connect-button")
    code = second_page.locator("#pair-code").inner_text().replace(" ", "")
    assert code.isdigit()
    assert len(code) == 6

    page.goto(alpha.url)
    page.click("#connect-button")
    page.click("[role=tab][data-tab=enter]")
    page.fill("#pair-input", code)
    page.locator("#pair-address-wrap summary").click()
    page.fill("#pair-address", bravo.url.removeprefix("http://"))
    page.click("#pair-submit")
    expect(page.locator(".toast", has_text="Paired with Bravo")).to_be_visible()
    expect(tile(page, "Bravo")).to_have_class(re.compile(r"\bis-paired\b"), timeout=8000)
    # Bravo hears about it too, and its sheet (with the used code) closes.
    expect(second_page.locator(".toast", has_text="Paired with Alpha")).to_be_visible(timeout=8000)
    expect(second_page.locator("#connect-dialog")).to_be_hidden()
    second_page.click("#connect-button")
    expect(second_page.locator("#pair-code")).not_to_have_text(f"{code[:3]} {code[3:]}")


def test_rename_this_device(devices: list[Server], page) -> None:  # type: ignore[no-untyped-def]
    alpha = devices[0]
    page.goto(alpha.url)
    page.click("#me-name")
    page.fill("#rename-input", "Studio iMac")
    page.locator("#rename-form").get_by_role("button", name="Save").click()
    expect(page.locator("#me-name-text")).to_have_text("Studio iMac")
    assert _api(alpha, "/api/p2p/v1/info")["name"] == "Studio iMac"


def test_phone_browser_sends_through_the_app(devices: list[Server], browser, second_page) -> None:  # type: ignore[no-untyped-def]
    ip = network.primary_ip()
    if not ip:
        pytest.skip("needs a LAN address")
    alpha, bravo, _ = devices
    phone_ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
        user_agent="Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
        "SamsungBrowser/25.0 Chrome/121.0 Mobile Safari/537.36",
    )
    phone = phone_ctx.new_page()
    try:
        phone.goto(f"http://{ip}:{alpha.url.rsplit(':', 1)[1]}")
        expect(phone.locator("#me-name-text")).to_have_text("Galaxy phone")
        expect(tile(phone, "Alpha")).to_be_visible()
        expect(tile(phone, "Bravo")).to_be_visible(timeout=10_000)
        second_page.goto(bravo.url)
        expect(tile(second_page, "Galaxy phone")).to_be_visible(timeout=15_000)

        tile(phone, "Bravo").locator("button").click()
        phone.set_input_files(
            "#file-input",
            files=[{"name": "IMG_0001.jpg", "mimeType": "image/jpeg", "buffer": b"jpeg" * 1000}],
        )
        phone.click("#send-go")
        second_page.locator("#incoming-accept").click(timeout=10_000)
        expect(phone.locator(".job-target", has_text="Bravo")).to_contain_text(
            "Delivered", timeout=10_000
        )
        assert (bravo.root / "IMG_0001.jpg").read_bytes() == b"jpeg" * 1000
        assert not (alpha.root / "IMG_0001.jpg").exists()
    finally:
        phone_ctx.close()


def _browser_page(browser, name: str, ua: str):  # type: ignore[no-untyped-def]
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844}, user_agent=ua, accept_downloads=True
    )
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    pg._errors = errors  # type: ignore[attr-defined]
    return ctx, pg


PIXEL = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36"
IPAD = "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"


@pytest.mark.parametrize("webrtc", [True, False])
def test_browsers_send_to_each_other_directly(
    devices: list[Server], browser, tmp_path: Path, webrtc: bool
) -> None:  # type: ignore[no-untyped-def]
    ip = network.primary_ip()
    if not ip:
        pytest.skip("needs a LAN address")
    alpha = devices[0]
    url = f"http://{ip}:{alpha.url.rsplit(':', 1)[1]}"
    ctx_a, phone = _browser_page(browser, "phone", PIXEL)
    ctx_b, ipad = _browser_page(browser, "ipad", IPAD)
    try:
        if not webrtc:  # the receiver can't do WebRTC: the sender must fall back to the app
            ipad.add_init_script("delete window.RTCPeerConnection;")
        phone.goto(url)
        ipad.goto(url)
        expect(tile(phone, "iPad")).to_be_visible(timeout=10_000)
        payload = bytes(range(256)) * 4000  # ~1 MB, several data-channel chunks
        tile(phone, "iPad").locator("button").click()
        phone.set_input_files(
            "#file-input",
            files=[{"name": "photo.heic", "mimeType": "image/heic", "buffer": payload}],
        )
        phone.click("#send-go")
        ipad.locator("#incoming-accept").click(timeout=10_000)
        expected = "Delivered directly" if webrtc else "Delivered"
        target = phone.locator(".job-target", has_text="iPad")
        expect(target).to_contain_text(expected, timeout=30_000)
        if webrtc:
            expect(target).to_contain_text("Delivered directly")
        else:
            expect(target).not_to_contain_text("directly")
        row = ipad.locator(".file-row", has_text="photo.heic")
        expect(row).to_be_visible(timeout=10_000)
        if webrtc:
            expect(row).to_contain_text("in this browser")
        with ipad.expect_download() as info:
            row.locator(".row-actions a").click(force=True)
        assert Path(info.value.path()).read_bytes() == payload
        # Nothing was stored on Alpha's disk either way.
        assert not (alpha.root / "photo.heic").exists()
        assert phone._errors == []  # type: ignore[attr-defined]
        assert ipad._errors == []  # type: ignore[attr-defined]
    finally:
        ctx_a.close()
        ctx_b.close()
