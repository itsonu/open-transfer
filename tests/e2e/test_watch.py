"""Watch in the browser: a video on this device plays and seeks in another
device's browser, streamed with Range requests, without being sent first."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.test_ui import Server, _start, sync_api
from tests.test_mesh import lan_ip

pytestmark = pytest.mark.e2e
expect = sync_api.expect


@pytest.fixture
def mac(tmp_path: Path) -> Iterator[Server]:
    """An app on the network with an MKV, an MP4, a file that isn't a video, and one not shared."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("needs ffmpeg to make a real video")
    gen = _start(tmp_path, name="Mac", owner=True, host="0.0.0.0")
    server = next(gen)
    server.root.mkdir(parents=True, exist_ok=True)
    film = server.root / "film.mkv"
    subprocess.run(  # 20 s of H.264/AAC at 4 Mbit/s: ~10 MB, more than a browser reads at once
        [ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440", "-t", "20", "-c:v", "libx264",
         "-preset", "ultrafast", "-b:v", "4M", "-g", "25", "-c:a", "aac", str(film)],
        check=True,
    )  # fmt: skip
    subprocess.run(
        [ffmpeg, "-nostdin", "-v", "error", "-i", str(film), "-c", "copy", "-movflags", "+faststart",
         str(server.root / "clip.mp4")],
        check=True,
    )  # fmt: skip
    (server.root / "broken.mkv").write_bytes(os.urandom(200_000))
    shutil.copy(server.root / "clip.mp4", server.root / "private.mp4")
    yield server
    for _ in gen:
        pass


def share(page, name: str) -> None:  # type: ignore[no-untyped-def]
    row = page.locator(".file-row", has_text=name)
    row.locator(".row-more").click()
    page.get_by_role("menuitem", name="Share to watch").click()
    expect(row.locator(".row-sub")).to_contain_text("shared to watch")


def test_watch_streams_seeks_and_says_when_it_cant(mac: Server, page, second_page) -> None:  # type: ignore[no-untyped-def]
    page.goto(mac.url)
    for name in ("film.mkv", "clip.mp4", "broken.mkv"):
        share(page, name)

    visitor = second_page
    ranges: list[tuple[str, str, int]] = []
    visitor.on(
        "response",
        lambda r: (
            "/api/watch/" in r.url
            and ranges.append((r.url, r.request.headers.get("range", ""), r.status))
        ),
    )
    visitor.goto(f"http://{lan_ip()}:{mac.url.rsplit(':', 1)[1]}")
    rows = visitor.locator("#file-list .file-row .row-title")
    expect(rows).to_have_count(3)
    assert sorted(rows.all_inner_texts()) == [
        "broken.mkv",
        "clip.mp4",
        "film.mkv",
    ]  # not private.mp4
    expect(
        visitor.locator(".file-row", has_text="film.mkv").get_by_role(
            "button", name="Delete film.mkv"
        )
    ).to_have_count(0)

    for name in ("film.mkv", "clip.mp4"):
        ranges.clear()
        visitor.locator(".file-row", has_text=name).locator(".row-more").click()
        visitor.get_by_role("menuitem", name="Preview").click()
        video = visitor.locator("#preview-stage video")
        expect(video).to_be_visible()
        video.evaluate(
            "v => new Promise((ok, fail) => { v.onerror = () => fail(v.error.message);"
            " v.readyState >= 1 ? ok() : (v.onloadedmetadata = ok); })"
        )
        assert video.evaluate("v => [Math.round(v.duration), v.videoWidth]") == [20, 640]
        video.evaluate("v => new Promise(ok => { v.onseeked = ok; v.currentTime = 17; })")
        video.evaluate("v => new Promise(ok => v.readyState >= 2 ? ok() : (v.oncanplay = ok))")
        assert video.evaluate("v => v.currentTime") == pytest.approx(17, abs=1)
        later = [r for r in ranges if r[1] and not r[1].startswith("bytes=0-")]
        assert later, f"{name}: no Range request past the start: {ranges}"
        assert all(status == 206 for _, _, status in later), ranges
        visitor.locator("#preview-close").click()

    visitor.locator(".file-row", has_text="broken.mkv").locator(".row-more").click()
    visitor.get_by_role("menuitem", name="Preview").click()
    stage = visitor.locator("#preview-stage")
    expect(stage.get_by_role("alert")).to_contain_text("This browser can’t play this video.")
    expect(stage).to_contain_text("MKV videos often play in Chrome or Edge, not in Safari")
    expect(stage.get_by_role("link", name="Save")).to_be_visible()
    expect(stage.locator("video")).to_have_count(0)

    # Sharing stops: the video leaves the visitor's list.
    page.locator(".file-row", has_text="clip.mp4").locator(".row-more").click()
    page.get_by_role("menuitem", name="Stop sharing to watch").click()
    visitor.locator("#preview-close").click()
    expect(rows).to_have_count(2)
