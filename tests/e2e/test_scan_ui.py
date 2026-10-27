"""Scanning another device's QR code in Add device: any camera, any browser engine."""

from __future__ import annotations

import base64
import io
import json
import re
import urllib.request

import pytest
import segno

from tests.e2e.test_ui import Server, sync_api

pytestmark = pytest.mark.e2e
expect = sync_api.expect

# A stand-in for getUserMedia: a canvas stream showing `window.__qr` (an image
# URL), or a rejection named by `window.__cameraError`. Every stream handed out
# is kept so a test can check the camera was let go.
FAKE_CAMERA = """
window.__streams = [];
window.__cameras = window.__cameras || 1;
navigator.mediaDevices.enumerateDevices = async () =>
  Array.from({ length: window.__cameras }, (_, i) =>
    ({ kind: "videoinput", deviceId: "cam" + i, label: "Camera " + i, groupId: "" }));
navigator.mediaDevices.getUserMedia = async (constraints) => {
  window.__asked = (window.__asked || []).concat([JSON.stringify(constraints)]);
  if (window.__cameraDelay) await new Promise((r) => setTimeout(r, window.__cameraDelay));
  if (window.__cameraError) throw new DOMException("no", window.__cameraError);
  const canvas = document.createElement("canvas");
  canvas.width = 480;
  canvas.height = 480;
  const g = canvas.getContext("2d");
  g.fillStyle = "#fff";
  g.fillRect(0, 0, 480, 480);
  if (window.__qr) {
    const img = new Image();
    img.src = window.__qr;
    await img.decode();
    g.imageSmoothingEnabled = false;
    g.drawImage(img, 40, 40, 400, 400);
  }
  const stream = canvas.captureStream(10);
  window.__streams.push(stream);
  return stream;
};
"""


def _open(server: Server, path: str) -> dict:  # type: ignore[type-arg]
    req = urllib.request.Request(f"{server.url}{path}", method="POST", data=b"{}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as res:
        return json.loads(res.read())  # type: ignore[no-any-return]


def _qr_png(text: str) -> str:
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=8, border=4)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _scan_sheet(page, server: Server, setup: str = "") -> None:  # type: ignore[no-untyped-def]
    page.add_init_script(setup + FAKE_CAMERA)
    page.goto(server.url)
    page.click("#connect-button")
    page.click("[role=tab][data-tab=enter]")


def _camera_released(page) -> bool:  # type: ignore[no-untyped-def]
    return bool(
        page.evaluate(
            "() => window.__streams.every((s) => s.getTracks().every((t) => t.readyState === 'ended'))"
        )
    )


def test_scan_pairs_without_barcode_detector(devices: list[Server], page) -> None:  # type: ignore[no-untyped-def]
    """Safari, Firefox, the Mac and Windows desktop windows have no BarcodeDetector."""
    _, bravo, charlie = devices
    code = _open(bravo, "/api/pair/open")["code"]
    qr = _qr_png(f"{bravo.url}/?pair={code}")
    _scan_sheet(page, charlie, f"delete window.BarcodeDetector; window.__qr = {json.dumps(qr)};")
    expect(page.locator("#scan-button")).to_be_visible()
    page.click("#scan-button")
    expect(page.locator(".toast", has_text="Paired with Bravo")).to_be_visible(timeout=10000)
    assert _camera_released(page)


def test_closing_the_sheet_while_the_camera_starts_releases_it(devices: list[Server], page) -> None:  # type: ignore[no-untyped-def]
    _scan_sheet(page, devices[0], "window.__cameraDelay = 800;")  # e.g. the permission prompt
    page.click("#scan-button")
    page.keyboard.press("Escape")
    expect(page.locator("#connect-dialog")).to_be_hidden()
    page.wait_for_function("() => window.__streams.length === 1")
    page.wait_for_timeout(200)
    assert _camera_released(page)


def test_a_second_tap_doesnt_open_a_second_camera(devices: list[Server], page) -> None:  # type: ignore[no-untyped-def]
    _scan_sheet(page, devices[0], "window.__cameraDelay = 300;")
    page.click("#scan-button")
    page.evaluate("() => document.querySelector('#scan-button').click()")
    page.wait_for_timeout(800)
    assert page.evaluate("() => window.__streams.length") == 1
    page.click("#scan-cancel")
    assert _camera_released(page)


@pytest.mark.parametrize(
    ("error", "says"),
    [
        ("NotAllowedError", r"[Aa]llow"),
        ("NotFoundError", r"No camera"),
        ("NotReadableError", r"in use"),
    ],
)
def test_camera_problems_say_what_happened_and_keep_the_code_box(
    devices: list[Server], page, error: str, says: str
) -> None:  # type: ignore[no-untyped-def]
    _scan_sheet(page, devices[0], f"window.__cameraError = {json.dumps(error)};")
    page.click("#scan-button")
    expect(page.locator("#pair-error")).to_have_text(re.compile(says))
    expect(page.locator("#pair-error")).to_contain_text("code")
    expect(page.locator("#scan-view")).to_be_hidden()
    expect(page.locator("#pair-input")).to_be_focused()


def test_switch_to_another_camera(devices: list[Server], page) -> None:  # type: ignore[no-untyped-def]
    """A laptop's built-in camera and a USB one."""
    _scan_sheet(page, devices[0], "window.__cameras = 2;")
    expect(page.locator("#scan-switch")).to_be_hidden()
    page.click("#scan-button")
    expect(page.locator("#scan-switch")).to_be_visible()
    page.click("#scan-switch")
    page.wait_for_function("() => window.__streams.length === 2")
    asked = page.evaluate("() => window.__asked")
    assert '"exact":"cam1"' in asked[-1]
    assert page.evaluate(
        "() => window.__streams[0].getTracks().every((t) => t.readyState === 'ended')"
    )
    page.click("#scan-cancel")
    assert _camera_released(page)


def test_one_camera_shows_no_switch(devices: list[Server], page) -> None:  # type: ignore[no-untyped-def]
    _scan_sheet(page, devices[0])
    page.click("#scan-button")
    expect(page.locator("#scan-view")).to_be_visible()
    expect(page.locator("#scan-switch")).to_be_hidden()
    page.click("#scan-cancel")
    assert _camera_released(page)
