"""Shared browser fixtures for the end-to-end tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="session")
def browser() -> Iterator[object]:
    with sync_api.sync_playwright() as p:
        try:
            # Local test servers only: never route them through an HTTP proxy.
            b = p.chromium.launch(args=["--no-proxy-server"])
        except Exception as exc:  # pragma: no cover - depends on the machine
            pytest.skip(f"Chromium not available: {exc}")
            return
        yield b
        b.close()


@pytest.fixture
def page(browser):  # type: ignore[no-untyped-def]
    context = browser.new_context(accept_downloads=True)
    pg = context.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    # Expected HTTP errors (e.g. a wrong PIN) are logged by Chromium as "Failed to load resource".
    pg.on(
        "console",
        lambda msg: (
            msg.type == "error"
            and "Failed to load resource" not in msg.text
            and errors.append(msg.text)
        ),
    )
    yield pg
    context.close()
    assert errors == [], f"browser errors: {errors}"
