"""Record the live part of the demo video (see ``build.py``).

Starts four real devices in this process (a Mac, a Windows PC, a Galaxy Tab and
a Pixel phone) and puts the Mac's and the Pixel's real web app side by side on
a 1920x1080 stage (``stage.html``). A script then plays the story: drop files,
pick two devices, send, accept on the phone, both delivered. The browser's
uploads are throttled to a Wi-Fi-like speed so the progress is visible.

Frames come from Chromium's screencast with their timestamps and are resampled
to a constant 30 fps. Writes ``<work>/frames/*.jpg`` and ``<work>/events.json``
(when each beat happened, for the soundtrack).
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import FrameLocator, Locator, Page, Route, sync_playwright

from open_transfer.config import Config
from open_transfer.node import Node

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FPS = 30
RATE = 12_000_000  # upload bytes per second: a decent home Wi-Fi

DEVICES: list[tuple[str, dict[str, Any]]] = [
    ("MacBook Pro", {"device_platform": "macos"}),
    ("Gaming PC", {"device_platform": "windows"}),
    ("Galaxy Tab S9", {"device_platform": "android", "device_form": "tablet"}),
    ("Pixel 8", {"device_platform": "android", "device_form": "phone"}),
]
TILES = (("desktop", "Windows"), ("laptop", "Mac"), ("phone", "Android"), ("tablet", "Tablet"))


def api(node: Node, path: str, method: str = "GET") -> dict[str, Any]:
    request = urllib.request.Request(f"{node.local_url}{path}", method=method)  # noqa: S310 - local test server
    with urllib.request.urlopen(request, timeout=5) as res:  # noqa: S310 - local test server
        return json.loads(res.read() or b"{}")  # type: ignore[no-any-return]


def build_stage(work: Path, mac: Node, pixel: Node) -> Path:
    index = (ROOT / "src/open_transfer/templates/index.html").read_text(encoding="utf-8")
    icons = "".join(
        m.group(0)
        for m in re.finditer(r'<symbol id="i-(?:phone|tablet|laptop|desktop)".*?</symbol>', index)
    )
    tiles = "".join(
        f'<div class="dev in"><div><svg><use href="#i-{icon}"/></svg><em>{name}</em></div></div>'
        for icon, name in TILES
    )
    html = (HERE / "stage.html").read_text(encoding="utf-8")
    html = (
        html.replace("__MAC__", mac.local_url)
        .replace("__PIXEL__", pixel.local_url)
        .replace("__LOGO__", (ROOT / "src/open_transfer/static/icons/logo.svg").as_uri())
        .replace("__DEVICES__", tiles)
        .replace("__SPRITE__", icons)
    )
    out = work / "stage.html"
    out.write_text(html, encoding="utf-8")
    return out


def unframe(route: Route) -> None:
    """Let the app's pages sit in the stage's iframes (they refuse framing)."""
    res = route.fetch()
    headers = {k: v for k, v in res.headers.items() if k.lower() != "x-frame-options"}
    if "content-security-policy" in headers:
        headers["content-security-policy"] = re.sub(
            r"frame-ancestors[^;]*;?", "", headers["content-security-policy"]
        )
    route.fulfill(response=res, headers=headers)


def center(loc: Locator) -> tuple[float, float]:
    box = loc.bounding_box()
    assert box is not None, loc  # noqa: S101 - a missing element is a broken script
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def accept_on_tablet(tab: Node) -> None:
    """The tablet accepts on its own screen, off camera."""
    deadline = time.time() + 10
    while time.time() < deadline:
        incoming = api(tab, "/api/state").get("incoming") or []
        if incoming:
            time.sleep(1.2)
            api(tab, f"/api/incoming/{incoming[0]['id']}/accept", "POST")
            return
        time.sleep(0.1)


def play(
    page: Page, mac: FrameLocator, phone: FrameLocator, tab: Node, uploads: list[str]
) -> dict[str, float]:
    events: dict[str, float] = {}
    t0 = time.time()

    def at(t: float, label: str) -> None:
        wait = t0 + t - time.time()
        if wait > 0:
            page.wait_for_timeout(wait * 1000)
        events[label] = time.time() - t0

    def tap(loc: Locator) -> None:  # dispatch, so nothing scrolls the stage
        loc.dispatch_event("click")
        page.evaluate("document.scrollingElement.scrollTo(0, 0)")

    events["start"] = t0
    # 0-3 s: the hook card. Its text leaves first, then the card dissolves.
    at(2.55, "hook-out")
    page.evaluate(
        "document.querySelectorAll('#hook .in').forEach((el) => el.classList.remove('on'))"
    )
    at(3.0, "reveal")
    page.evaluate("stage.show('#hook', false)")
    at(3.4, "cap1")
    page.evaluate("stage.caption('Your devices find each other on Wi‑Fi.')")

    # A file flies in from the bottom-left and lands in the Mac's drop zone.
    dx, dy = center(mac.locator("#dropzone"))
    at(5.4, "cap2")
    page.evaluate("stage.caption('Drop files. Pick who gets them.')")
    page.evaluate(
        'stage.chip(\'f1\', \'<div class="ic" style="background:#8e5cf6">ZIP</div>'
        "<div>Wedding photos.zip<small>30 MB</small></div>', 120, 1100)"
    )
    page.evaluate(f"stage.move({dx - 80}, {dy + 40})")
    at(5.6, "fly")
    page.evaluate(f"stage.fly('f1', {dx - 160}, {dy - 30}, .9)")
    at(6.6, "drop")
    page.evaluate("stage.fade('f1')")
    mac.locator("#file-input").set_input_files(files=uploads)

    for label, name, t in (("pick1", "Pixel 8", 7.4), ("pick2", "Galaxy Tab", 8.1)):
        button = mac.locator(".device", has_text=name).locator("button").first
        x, y = center(button)
        at(t - 0.7, f"{label}-move")
        page.evaluate(f"stage.move({x}, {y})")
        at(t, label)
        page.evaluate(f"stage.press({x}, {y})")
        tap(button)
    go = mac.locator("#send-go")
    at(8.2, "send-move")
    x, y = center(go)
    page.evaluate(f"stage.move({x}, {y})")
    at(8.9, "send")
    page.evaluate(f"stage.press({x}, {y})")
    tap(go)
    threading.Thread(target=accept_on_tablet, args=(tab,), daemon=True).start()

    # The phone asks; we tap Accept.
    accept = phone.locator("#incoming-accept")
    accept.wait_for(timeout=10_000)
    events["incoming"] = time.time() - t0
    at(events["incoming"] + 0.25, "cap3")
    page.evaluate("stage.caption('They tap Accept.')")
    page.evaluate("stage.move(1990, 760)")  # the cursor leaves; the phone is touched
    page.wait_for_timeout(600)  # the dialog settles
    mac.locator(".job").first.evaluate(
        "el => el.scrollIntoView({behavior: 'smooth', block: 'center'})"
    )
    ax, ay = center(accept)
    at(events["cap3"] + 1.4, "accept")
    page.evaluate(f"stage.press({ax}, {ay}, true)")
    tap(accept)
    at(events["accept"] + 0.5, "cap4")
    page.evaluate("stage.caption('Straight from device to device. No cloud.')")

    delivered = mac.locator(".job").first.get_by_text("Delivered")
    for _ in range(240):
        if delivered.count() >= 2:
            break
        page.wait_for_timeout(50)
    else:
        raise RuntimeError("the transfer never reached both devices")
    events["delivered"] = time.time() - t0

    at(max(events["delivered"] + 1.8, events["cap4"] + 3.4), "outro")
    page.evaluate("stage.caption('')")
    page.evaluate("stage.show('#outro', true); setTimeout(() => stage.reveal('#outro', 170), 420)")
    at(events["outro"] + 4.9, "end")
    return events


def record(work: Path, chromium: str | None = None) -> dict[str, float]:
    work.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[float, bytes]] = []
    discovery_port = random.randint(40000, 59000)  # noqa: S311 - not security related
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
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
        mac, _pc, tab, pixel = nodes
        for name, size in (("Q3 Report.pdf", 2_412_000), ("Budget 2026.xlsx", 88_400)):
            with (Path(tmp) / "MacBook Pro" / name).open("wb") as fh:  # already received
                fh.truncate(size)
        uploads = []
        for name, size in (("Wedding photos.zip", 30_000_000), ("Boarding pass.pdf", 300_000)):
            path = Path(tmp) / "to-send" / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(os.urandom(size))
            uploads.append(str(path))

        browser = p.chromium.launch(
            executable_path=chromium,
            # one renderer for the stage and its frames, so the throttle covers both
            args=["--no-proxy-server", "--disable-site-isolation-trials"],
        )
        try:
            context = browser.new_context(viewport={"width": 1920, "height": 1080})
            context.route(
                lambda url: url.startswith("http://127.0.0.1") and url.rstrip("/").count("/") == 2,
                unframe,
            )
            page = context.new_page()
            page.goto(build_stage(work, mac, pixel).as_uri())
            mac_app, phone_app = page.frame_locator("#mac"), page.frame_locator("#pixel")
            mac_app.locator(".device").nth(2).wait_for(timeout=20_000)
            phone_app.locator(".device").nth(2).wait_for(timeout=20_000)
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(1500)

            cdp = context.new_cdp_session(page)

            def on_frame(event: dict[str, Any]) -> None:
                frames.append((event["metadata"]["timestamp"], base64.b64decode(event["data"])))
                cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})

            cdp.on("Page.screencastFrame", on_frame)
            cdp.send("Network.enable")
            cdp.send(
                "Network.emulateNetworkConditions",
                {
                    "offline": False,
                    "latency": 2,
                    "downloadThroughput": -1,
                    "uploadThroughput": RATE,
                },
            )
            page.evaluate("stage.reveal('#hook', 180)")
            cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "everyNthFrame": 1})
            events = play(page, mac_app, phone_app, tab, uploads)
            cdp.send("Page.stopScreencast")
        finally:
            browser.close()
            for node in nodes:
                node.stop()

    # Resample to a constant frame rate: each tick shows the newest frame so far.
    frames.sort(key=lambda f: f[0])
    out = work / "frames"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    start = events.pop("start")
    j = 0
    for i in range(round(events["end"] * FPS)):
        t = start + i / FPS
        while j + 1 < len(frames) and frames[j + 1][0] <= t:
            j += 1
        (out / f"{i:04d}.jpg").write_bytes(frames[j][1])
    (work / "events.json").write_text(json.dumps(events, indent=1), encoding="utf-8")
    return events
