"""Browser tests for activity (history grouped by day, search and filters),
the transfer queue, and how connections are shown."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.e2e.test_devices_ui import tile
from tests.e2e.test_ui import Server, sync_api

pytestmark = pytest.mark.e2e
expect = sync_api.expect


def send_file(pg, to: str, path: Path) -> None:  # type: ignore[no-untyped-def]
    tile(pg, to).locator(".device-button").click()
    pg.set_input_files("#file-input", str(path))
    pg.click("#send-go")


def test_activity_by_day_with_search_and_filters(
    devices: list[Server], page, second_page, third_page, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo, charlie = devices
    page.goto(alpha.url)
    second_page.goto(bravo.url)
    third_page.goto(charlie.url)
    expect(tile(page, "Charlie")).to_be_visible(timeout=10_000)
    for name, to, other in (
        ("alpha-notes.txt", "Bravo", second_page),
        ("plan.txt", "Charlie", third_page),
    ):
        path = tmp_path / name
        path.write_text(name)
        send_file(page, to, path)
        other.locator("#incoming-accept").click(timeout=10_000)
        expect(page.locator(".job", has_text=name).locator(".job-target")).to_contain_text(
            "Delivered", timeout=10_000
        )

    rows = page.locator("#history-list .history-row")
    expect(rows).to_have_count(2, timeout=10_000)
    expect(page.locator("#history-list .history-day").first).to_have_text("Today")
    expect(rows.first).to_contain_text("plan.txt")  # newest first
    expect(rows.first).to_contain_text("To Charlie")

    page.fill("#history-search", "notes")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("alpha-notes.txt")
    page.fill("#history-search", "")
    page.select_option("#history-device", label="Charlie")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("plan.txt")
    page.select_option("#history-device", value="")
    page.select_option("#history-direction", "received")
    expect(rows).to_have_count(0)
    expect(page.locator("#history-empty")).to_be_visible()

    # The receiver's activity shows it too, and survives a reload (it's kept on the device).
    second_page.reload()
    received = second_page.locator("#history-list .history-row", has_text="alpha-notes.txt")
    expect(received).to_be_visible(timeout=10_000)
    expect(received).to_contain_text("From Alpha")
    expect(received.locator(".history-dir")).to_have_text("Received")


def test_new_sends_wait_their_turn(
    devices: list[Server], page, second_page, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo, _ = devices
    page.goto(alpha.url)
    second_page.goto(bravo.url)
    expect(tile(page, "Bravo")).to_be_visible(timeout=10_000)
    for i in range(1, 5):
        path = tmp_path / f"file-{i}.txt"
        path.write_text(str(i))
        send_file(page, "Bravo", path)
    # Two at a time: 3 and 4 wait.
    queued = page.locator(".job.is-queued")
    expect(queued).to_have_count(2)
    expect(queued.nth(0)).to_contain_text("file-3.txt")
    expect(queued.nth(0)).to_contain_text("Queued")
    queued.nth(1).get_by_role("button", name=re.compile("Move .* up")).click()
    expect(queued.nth(0)).to_contain_text("file-4.txt")
    queued.nth(1).get_by_role("button", name=re.compile("Remove .* from the queue")).click()
    expect(queued).to_have_count(1)

    # Bravo answers the first offer; the next in line (file 4) starts.
    second_page.locator("#incoming-decline").click(timeout=10_000)
    expect(queued).to_have_count(0, timeout=10_000)
    expect(page.locator(".job", has_text="file-4.txt")).to_be_visible()
    second_page.locator("#incoming-accept").click(timeout=10_000)  # file 2
    second_page.locator("#incoming-accept").click(timeout=10_000)  # file 4
    expect(page.locator(".job", has_text="file-4.txt").locator(".job-target")).to_contain_text(
        "Delivered", timeout=10_000
    )
    assert not (bravo.root / "file-3.txt").exists()


def test_devices_and_transfers_show_their_connection(
    devices: list[Server], page, second_page, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    alpha, bravo, _ = devices
    page.goto(alpha.url)
    second_page.goto(bravo.url)
    signal = tile(page, "Bravo").locator(".device-signal")
    expect(signal).to_have_attribute(
        "aria-label", re.compile(r"^Connection: (good|fair|slow) \(\d+ ms\)$"), timeout=20_000
    )
    path = tmp_path / "path.txt"
    path.write_text("p")
    send_file(page, "Bravo", path)
    target = page.locator(".job", has_text="path.txt").locator(".job-target")
    expect(target).to_have_attribute("data-path", "app")
    expect(target.locator(".job-target-path")).to_have_text("Direct")
