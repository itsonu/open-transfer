"""--https in a real browser: the page, an upload and the status line go over TLS."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.test_ui import _start, sync_api

pytestmark = pytest.mark.e2e
expect = sync_api.expect


def test_a_browser_uploads_over_https(browser, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    starting = _start(tmp_path, "--https")
    server = next(starting)
    context = browser.new_context(ignore_https_errors=True)  # self-signed: the one warning
    try:
        page = context.new_page()
        port = int(server.url.rsplit(":", 1)[1]) + 1
        page.goto(f"https://127.0.0.1:{port}/")
        expect(page.locator("#connection")).to_have_attribute("data-state", "online")
        sample = tmp_path / "over-tls.txt"
        sample.write_bytes(b"x" * 100_000)
        page.set_input_files("#file-input", str(sample))
        expect(page.locator(".file-row", has_text="over-tls.txt")).to_be_visible()
        assert (server.root / "over-tls.txt").read_bytes() == sample.read_bytes()
        assert page.evaluate("window.isSecureContext") is True  # WebCrypto and friends work
    finally:
        context.close()
        starting.close()
