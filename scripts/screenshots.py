"""Regenerate the screenshots in docs/screenshots/.

    python scripts/screenshots.py

Needs the e2e extra (``pip install -e '.[e2e]'`` + ``playwright install chromium``).
Starts four real devices in this process (a Mac, a Windows PC, a Galaxy Tab and
a Pixel phone) that find each other on the local network, then captures the
main states in light and dark mode on desktop and phone sizes.
"""

from __future__ import annotations

import json
import os
import random
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

from open_transfer.config import Config
from open_transfer.node import Node

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"

PHOTO_HTML = """
<canvas id=c width=900 height=600></canvas><script>{{
const c = document.getElementById('c').getContext('2d');
const g = c.createLinearGradient(0, 0, 0, 600);
g.addColorStop(0, '{top}'); g.addColorStop(0.62, '{mid}'); g.addColorStop(1, '{bottom}');
c.fillStyle = g; c.fillRect(0, 0, 900, 600);
c.fillStyle = 'rgba(255,240,200,.95)'; c.beginPath(); c.arc(610, 330, 70, 0, 7); c.fill();
c.fillStyle = 'rgba(20,30,60,.55)'; c.fillRect(0, 400, 900, 200);
}}</script>"""

RECEIVED = [  # name, size in bytes (sparse), minutes ago
    ("Q3 Report.pdf", 2_412_000, 14),
    ("Budget 2026.xlsx", 88_400, 55),
    ("Design specs.zip", 48_200_000, 180),
]
PHOTOS = [
    ("Sunset at Big Sur.jpg", ("#ff9a5a", "#ff5e62", "#3b2c5a"), 3),
    ("Golden hour.jpg", ("#2c3e70", "#f7b267", "#1d2b53"), 40),
]
DEVICES: list[tuple[str, dict[str, Any]]] = [
    ("Sonu’s MacBook Pro", {"device_platform": "macos"}),
    ("Gaming PC", {"device_platform": "windows"}),
    ("Galaxy Tab S9", {"device_platform": "android", "device_form": "tablet"}),
    ("Pixel 8", {"device_platform": "android", "device_form": "phone"}),
]


def seed(share: Path, page: Page) -> None:
    share.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for name, size, minutes in RECEIVED:
        path = share / name
        with path.open("wb") as fh:
            fh.truncate(size)
        os.utime(path, (now - minutes * 60, now - minutes * 60))
    for name, (top, mid, bottom), minutes in PHOTOS:
        page.set_content(PHOTO_HTML.format(top=top, mid=mid, bottom=bottom))
        page.wait_for_timeout(100)
        path = share / name
        path.write_bytes(page.locator("#c").screenshot(type="jpeg", quality=85))
        os.utime(path, (now - minutes * 60, now - minutes * 60))


def api(node: Node, path: str, method: str = "GET") -> dict[str, Any]:
    request = urllib.request.Request(f"{node.local_url}{path}", method=method)  # noqa: S310 - local test server
    with urllib.request.urlopen(request, timeout=5) as res:  # noqa: S310 - local test server
        return json.loads(res.read() or b"{}")  # type: ignore[no-any-return]


def wait_devices(page: Page, count: int) -> None:
    # (wait_for_function would need eval, which the page's CSP forbids)
    page.locator(".device").nth(count - 1).wait_for(timeout=15_000)
    page.wait_for_timeout(600)


def wait_for_image(page: Page, selector: str) -> None:
    img = page.locator(selector)
    for _ in range(100):
        if img.evaluate("el => el.complete && el.naturalWidth > 0"):
            return
        page.wait_for_timeout(50)
    raise RuntimeError(f"{selector} never loaded")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    discovery_port = random.randint(40000, 59000)  # noqa: S311 - not security related
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-proxy-server"])
        seed(Path(tmp) / "Sonu’s MacBook Pro", browser.new_page())
        nodes = []
        for name, extra in DEVICES:
            config = Config(
                storage_dir=Path(tmp) / name,
                port=0,
                device_name=name,
                discovery_port=discovery_port,
                public_url="http://192.168.1.24:5000",
                reserve_disk_bytes=0,
                **extra,
            )
            node = Node(config)
            node.start()
            nodes.append(node)
        mac, pc, tab, phone = nodes
        try:
            for scheme in ("light", "dark"):
                ctx = browser.new_context(
                    viewport={"width": 1180, "height": 900},
                    device_scale_factor=2,
                    color_scheme=scheme,
                )
                page = ctx.new_page()
                page.goto(mac.local_url)
                wait_devices(page, 3)
                page.wait_for_selector(".file-row img.is-loaded")
                page.wait_for_timeout(400)
                page.screenshot(path=OUT / f"desktop-{scheme}.png")
                ctx.close()

            # Choosing recipients, then sending to two devices.
            ctx = browser.new_context(
                viewport={"width": 1180, "height": 900}, device_scale_factor=2
            )
            page = ctx.new_page()
            page.goto(mac.local_url)
            wait_devices(page, 3)
            page.locator(".device", has_text="Gaming PC").locator("button").click()
            page.locator(".device", has_text="Galaxy Tab").locator("button").click()
            page.set_input_files(
                "#file-input",
                files=[
                    {
                        "name": "Wedding photos.zip",
                        "mimeType": "application/zip",
                        "buffer": os.urandom(30_000_000),
                    },
                    {
                        "name": "Boarding pass.pdf",
                        "mimeType": "application/pdf",
                        "buffer": os.urandom(300_000),
                    },
                ],
            )
            page.wait_for_timeout(500)
            page.screenshot(path=OUT / "choose.png")
            page.click("#send-go")
            # The tablet answers on screen (captured below); the PC accepts right away.
            deadline = time.time() + 10
            while time.time() < deadline and not api(pc, "/api/state")["incoming"]:
                time.sleep(0.2)
            offer = api(pc, "/api/state")["incoming"][0]
            api(pc, f"/api/incoming/{offer['id']}/accept", "POST")

            tab_ctx = browser.new_context(
                viewport={"width": 820, "height": 1180},
                device_scale_factor=2,
                is_mobile=True,
                has_touch=True,
            )
            tab_page = tab_ctx.new_page()
            tab_page.goto(tab.local_url)
            tab_page.wait_for_selector("#incoming-dialog[open]", timeout=10_000)
            tab_page.wait_for_timeout(500)
            tab_page.screenshot(path=OUT / "incoming.png")
            page.wait_for_timeout(1500)
            page.screenshot(path=OUT / "sending.png")
            tab_page.click("#incoming-accept")
            page.wait_for_timeout(2500)
            tab_ctx.close()

            # Add a device: QR + pairing code.
            page.goto(mac.local_url)
            wait_devices(page, 3)
            page.click("#connect-button")
            wait_for_image(page, "#qr-image")
            page.wait_for_timeout(500)
            page.screenshot(path=OUT / "connect-light.png")
            ctx.close()

            for scheme in ("light", "dark"):
                ctx = browser.new_context(
                    viewport={"width": 393, "height": 852},
                    device_scale_factor=3,
                    is_mobile=True,
                    has_touch=True,
                    color_scheme=scheme,
                )
                page = ctx.new_page()
                page.goto(phone.local_url)
                wait_devices(page, 3)
                page.screenshot(path=OUT / f"phone-{scheme}.png")
                ctx.close()
        finally:
            for node in nodes:
                node.stop()
            browser.close()
    for stale in ("phone-pin.png",):
        (OUT / stale).unlink(missing_ok=True)
    print(f"Saved screenshots to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
