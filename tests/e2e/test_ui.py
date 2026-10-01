"""Browser end-to-end tests against a real server process.

Run with ``pytest -m e2e`` (needs ``pip install -e '.[e2e]'`` and
``playwright install chromium``). Skipped automatically when Playwright or a
browser is not available.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from open_transfer.network import find_free_port

sync_api = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.e2e


class Server:
    def __init__(self, url: str, root: Path, proc: subprocess.Popen[bytes]) -> None:
        self.url, self.root, self.proc = url, root, proc


def _start(
    tmp_path: Path, *args: str, name: str = "share", owner: bool = False, host: str = "127.0.0.1"
) -> Iterator[Server]:
    """Start the CLI. By default the browser is a *visitor* in shared-folder mode
    (the classic experience); ``owner=True`` makes 127.0.0.1 the device's owner."""
    port = find_free_port(host, 18000)
    root = tmp_path / name
    env = {**os.environ, "NO_COLOR": "1", "OPEN_TRANSFER_OWNER_LOOPBACK": "1" if owner else "0"}
    mode = [] if owner else ["--share-folder"]
    proc = subprocess.Popen(
        [sys.executable, "-m", "open_transfer", str(root), "--host", host,
         "--port", str(port), "--no-browser", "--no-qr", "--no-discovery", "--name", name,
         *mode, *args],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )  # fmt: skip
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while True:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1):
                break
        except OSError:
            if time.time() > deadline or proc.poll() is not None:
                proc.kill()
                out = proc.stdout.read().decode() if proc.stdout else ""
                raise RuntimeError(f"server did not start:\n{out}") from None
            time.sleep(0.1)
    try:
        yield Server(url, root, proc)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    yield from _start(tmp_path)


def test_send_download_delete_undo(server: Server, page, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    expect = sync_api.expect
    sample = tmp_path / "Holiday photo.txt"
    sample.write_bytes(b"x" * 250_000)

    page.goto(server.url)
    expect(page.locator("#connection")).to_have_attribute("data-state", "online")
    expect(page.locator("#empty")).to_be_visible()

    page.set_input_files("#file-input", str(sample))
    row = page.locator(".file-row", has_text="Holiday photo.txt")
    expect(row).to_be_visible()
    expect(page.locator(".toast", has_text="Sent")).to_be_visible()
    assert (server.root / "Holiday photo.txt").stat().st_size == 250_000

    with page.expect_download() as download_info:
        row.get_by_role("link", name="Download Holiday photo.txt").click()
    downloaded = download_info.value
    assert downloaded.suggested_filename == "Holiday photo.txt"
    assert Path(downloaded.path()).read_bytes() == sample.read_bytes()

    row.hover()
    row.get_by_role("button", name="Delete Holiday photo.txt").click()
    expect(row).to_have_count(0)
    assert not (server.root / "Holiday photo.txt").exists()
    page.locator(".toast").get_by_role("button", name="Undo").click()
    expect(page.locator(".file-row", has_text="Holiday photo.txt")).to_be_visible()
    assert (server.root / "Holiday photo.txt").exists()


def test_files_from_other_devices_appear(server: Server, page) -> None:  # type: ignore[no-untyped-def]
    page.goto(server.url)
    sync_api.expect(page.locator("#empty")).to_be_visible()
    req = urllib.request.Request(
        f"{server.url}/api/files",
        data=b"hi",
        method="POST",
        headers={"X-Filename": "from-phone.txt"},
    )
    urllib.request.urlopen(req, timeout=5).close()
    sync_api.expect(page.locator(".file-row", has_text="from-phone.txt")).to_be_visible(
        timeout=8000
    )


def test_connect_sheet(server: Server, page) -> None:  # type: ignore[no-untyped-def]
    page.goto(server.url)
    page.locator("#connect-button").click()
    dialog = page.locator("#connect-dialog")
    sync_api.expect(dialog).to_be_visible()
    sync_api.expect(page.locator("#share-url")).to_contain_text("http://")
    qr = page.locator("#qr-image")
    deadline = time.time() + 5
    while not qr.evaluate("el => el.complete && el.naturalWidth > 0"):
        assert time.time() < deadline, "QR code never loaded"
        page.wait_for_timeout(50)
    page.keyboard.press("Escape")
    sync_api.expect(dialog).to_be_hidden()


def test_pin_flow(tmp_path: Path, page) -> None:  # type: ignore[no-untyped-def]
    for srv in _start(tmp_path, "--pin", "2468"):
        page.goto(srv.url)
        sync_api.expect(page.locator("#lock-view")).to_be_visible()
        page.fill("#pin-input", "1111")
        page.click("#pin-submit")
        sync_api.expect(page.locator("#pin-error")).to_contain_text("isn't right")
        page.fill("#pin-input", "2468")
        page.click("#pin-submit")
        sync_api.expect(page.locator("#main-view")).to_be_visible()
        sync_api.expect(page.locator("#empty")).to_be_visible()


def test_receive_only(tmp_path: Path, page) -> None:  # type: ignore[no-untyped-def]
    for srv in _start(tmp_path, "--receive-only"):
        page.goto(srv.url)
        sync_api.expect(page.locator("#receive-only")).to_be_visible()
        sync_api.expect(page.locator("#dropzone")).to_be_visible()


def test_list_survives_a_file_growing_on_disk(server: Server, page) -> None:  # type: ignore[no-untyped-def]
    # e.g. the owner copies a big file into the folder: it's the first row and its
    # size changes between polls, so the row is rebuilt in place.
    (server.root / "older.txt").write_bytes(b"x")
    growing = server.root / "copying.bin"
    growing.write_bytes(b"x" * 1000)
    page.goto(server.url)
    sync_api.expect(page.locator(".file-row")).to_have_count(2)
    with growing.open("ab") as fh:
        fh.write(b"x" * 2_000_000)
    # A rendering error would be caught by the poller and shown as "Reconnecting…",
    # so watch the connection state closely while the row is rebuilt.
    states = set()
    deadline = time.time() + 6
    while time.time() < deadline:
        states.add(page.locator("#connection").get_attribute("data-state"))
        page.wait_for_timeout(100)
    assert states == {"online"}
    sync_api.expect(page.locator(".file-row", has_text="copying.bin")).to_contain_text("2.0 MB")
    sync_api.expect(page.locator(".file-row")).to_have_count(2)
