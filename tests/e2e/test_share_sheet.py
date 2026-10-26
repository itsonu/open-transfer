"""Images and files shared to Open Transfer from Android's share sheet.

The Android app hands the page what was shared through its native bridge
(``OpenTransferAndroid.takeShared()``) and serves the bytes itself on a
same-origin ``/__shared/…`` URL (see SharedFiles.kt). Here a fake bridge and
a routed URL stand in for the phone; the rest — staging, choosing a device,
sending, the other device saving it — is the real thing.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e.test_devices_ui import tile
from tests.e2e.test_ui import Server, sync_api

pytestmark = pytest.mark.e2e
expect = sync_api.expect

PHOTO = b"\xff\xd8\xff\xe0" + b"p" * 40_000
PDF = b"%PDF-1.7\n" + b"d" * 9_000

FAKE_BRIDGE = """
window.__shares = [];
window.__taken = 0;
window.OpenTransferAndroid = {
  takeShared() {
    window.__taken += 1;
    const next = window.__shares.shift();
    return next ? JSON.stringify(next) : "";
  },
};
"""


def share(page, *files: dict) -> None:  # type: ignore[no-untyped-def,type-arg]
    """What the app does when something is shared while the window is open."""
    page.evaluate(
        "(files) => { window.__shares.push({ files });"
        " window.dispatchEvent(new Event('ot-native-share')); }",
        list(files),
    )


@pytest.fixture
def phone(page, devices: list[Server]):  # type: ignore[no-untyped-def]
    served: list[str] = []

    def fulfil(route) -> None:  # type: ignore[no-untyped-def]
        url = route.request.url
        served.append(url)
        if url.endswith("/0"):
            route.fulfill(status=200, body=PHOTO, content_type="image/jpeg")
        elif url.endswith("/1"):
            route.fulfill(status=200, body=PDF, content_type="application/pdf")
        else:
            route.fulfill(status=404, body="")

    page.add_init_script(FAKE_BRIDGE)
    page.route("**/__shared/**", fulfil)
    page.goto(devices[0].url)
    expect(tile(page, "Bravo")).to_be_visible(timeout=10_000)
    page.served = served
    return page


PHOTO_ITEM = {"url": "/__shared/t0k3n/0", "name": "IMG_2041.jpg", "mime": "image/jpeg"}
PDF_ITEM = {"url": "/__shared/t0k3n/1", "name": "Boarding pass.pdf", "mime": "application/pdf"}


def test_shared_files_are_ready_to_send(phone, second_page, devices: list[Server]) -> None:  # type: ignore[no-untyped-def]
    _alpha, bravo, charlie = devices
    second_page.goto(bravo.url)
    share(phone, PHOTO_ITEM, PDF_ITEM)
    expect(phone.locator("#send-bar-files")).to_contain_text("2 files")
    expect(phone.locator("#toasts")).to_contain_text("choose who to send to")

    tile(phone, "Bravo").locator(".device-button").click()
    phone.click("#send-go")
    second_page.locator("#incoming-accept").click(timeout=10_000)
    expect(phone.locator(".job-target", has_text="Bravo")).to_contain_text(
        "Delivered", timeout=10_000
    )
    assert (bravo.root / "IMG_2041.jpg").read_bytes() == PHOTO
    assert (bravo.root / "Boarding pass.pdf").read_bytes() == PDF
    assert not (charlie.root / "IMG_2041.jpg").exists()


def test_a_share_that_arrived_before_the_page_is_picked_up(page, devices: list[Server]) -> None:  # type: ignore[no-untyped-def]
    # Shared with the app closed: the bridge already holds it when the page loads.
    page.add_init_script(
        FAKE_BRIDGE + f"window.__shares.push({json.dumps({'files': [PHOTO_ITEM]})});"
    )
    page.route("**/__shared/**", lambda route: route.fulfill(status=200, body=PHOTO))
    page.goto(devices[0].url)
    expect(page.locator("#send-bar-files")).to_contain_text("IMG_2041.jpg", timeout=10_000)
    assert page.evaluate("window.__taken") == 1  # the app hands each share over once


def test_a_file_that_cant_be_read_is_reported(phone) -> None:  # type: ignore[no-untyped-def]
    share(
        phone,
        PHOTO_ITEM,
        {"url": "/__shared/t0k3n/9", "name": "gone.mov", "mime": "video/quicktime"},
    )
    expect(phone.locator("#send-bar-files")).to_contain_text("IMG_2041.jpg")
    expect(phone.locator("#send-bar-files")).not_to_contain_text("gone.mov")
    expect(phone.locator("#toasts")).to_contain_text("Couldn’t read 1 shared file")


def test_only_the_bridge_url_is_fetched(phone) -> None:  # type: ignore[no-untyped-def]
    # A page that isn't this app's own can't use the bridge to read other URLs.
    share(phone, {"url": "https://example.com/x", "name": "x.bin", "mime": ""}, PHOTO_ITEM)
    expect(phone.locator("#send-bar-files")).to_contain_text("IMG_2041.jpg")
    expect(phone.locator("#send-bar-files")).not_to_contain_text("x.bin")
    assert all("/__shared/" in url for url in phone.served)


def test_shared_text_files_keep_their_name(phone) -> None:  # type: ignore[no-untyped-def]
    phone.unroute("**/__shared/**")
    phone.route("**/__shared/**", lambda route: route.fulfill(status=200, body="hello"))
    share(phone, {"url": "/__shared/t/0", "name": "notes.txt", "mime": "text/plain"})
    expect(phone.locator("#send-bar-files")).to_contain_text("notes.txt")


def test_browsers_without_the_bridge_are_unaffected(page, devices: list[Server]) -> None:  # type: ignore[no-untyped-def]
    page.goto(devices[0].url)
    expect(tile(page, "Bravo")).to_be_visible(timeout=10_000)
    page.evaluate("window.dispatchEvent(new Event('ot-native-share'))")
    expect(page.locator("#send-bar")).to_be_hidden()
