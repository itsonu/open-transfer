"""A big upload that was cut off (page reloaded, browser closed) carries on from the
pieces the device already has when the same file is chosen again."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.test_ui import Server, _start, sync_api

pytestmark = pytest.mark.e2e
expect = sync_api.expect

PIECE = 2 * 1024 * 1024


@pytest.fixture
def device(tmp_path: Path) -> Iterator[Server]:
    yield from _start(tmp_path)


def cut_off_after_two(page, device: Server, film: Path) -> None:  # type: ignore[no-untyped-def]
    """Start uploading ``film`` and drop the connection once two pieces have landed."""
    page.goto(device.url)
    expect(page.locator("#connection")).to_have_attribute("data-state", "online")
    landed = {"n": 0}

    def cut(route) -> None:  # type: ignore[no-untyped-def]
        if route.request.method == "POST" and route.request.headers.get("content-range"):
            landed["n"] += 1
            if landed["n"] > 2:
                route.abort()  # the connection drops
                return
        route.continue_()

    page.route("**/api/files", cut)
    page.set_input_files("#file-input", str(film))
    expect(page.get_by_role("button", name=f"Retry {film.name}")).to_be_visible(timeout=60_000)
    page.unroute("**/api/files")


def pieces_sent_choosing_again(page, device: Server, film: Path) -> int:  # type: ignore[no-untyped-def]
    """Reload the page, choose ``film`` again and count the pieces that go up."""
    page.reload()  # the page forgets the file; the device keeps what it has
    expect(page.locator("#connection")).to_have_attribute("data-state", "online")
    sent: list[str] = []
    page.on(
        "request",
        lambda r: r.method == "POST" and r.headers.get("content-range") and sent.append("piece"),
    )
    page.set_input_files("#file-input", str(film))
    expect(page.locator(".file-row", has_text=film.name)).to_be_visible(timeout=30_000)
    assert (device.root / film.name).read_bytes() == film.read_bytes()
    return len(sent)


def six_pieces(tmp_path: Path) -> Path:
    film = tmp_path / "film.bin"
    film.write_bytes(os.urandom(6 * PIECE))
    return film


def test_choosing_the_same_big_file_again_sends_only_what_is_missing(
    page, device: Server, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    film = six_pieces(tmp_path)
    cut_off_after_two(page, device, film)
    assert pieces_sent_choosing_again(page, device, film) == 4
    assert json.loads(page.evaluate("localStorage.getItem('ot-resume') || '{}'")) == {}


def test_dismissing_a_cut_off_upload_starts_the_file_over(
    page, device: Server, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    film = six_pieces(tmp_path)
    cut_off_after_two(page, device, film)
    page.get_by_role("button", name="Dismiss film.bin").click()
    assert pieces_sent_choosing_again(page, device, film) == 6
