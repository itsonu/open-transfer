"""Shared browser fixtures for the end-to-end tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from tests.e2e.test_ui import Server, _start  # noqa: E402 - needs Playwright


@pytest.fixture(scope="session")
def browser() -> Iterator[object]:
    with sync_api.sync_playwright() as p:
        try:
            # Local test servers only: never route them through an HTTP proxy.
            # OT_CHROMIUM: use a specific Chromium binary instead of Playwright's own.
            b = p.chromium.launch(
                args=["--no-proxy-server"], executable_path=os.environ.get("OT_CHROMIUM") or None
            )
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


# Nearby devices: real ``open-transfer`` apps whose owners' windows are pages on
# 127.0.0.1, linked with ``--peer`` so the tests don't depend on multicast.


@pytest.fixture
def devices(tmp_path: Path) -> Iterator[list[Server]]:
    """Three apps: Alpha, then Bravo and Charlie which connect to Alpha."""
    started: list[Iterator[Server]] = []
    servers: list[Server] = []
    gen = _start(tmp_path, name="Alpha", owner=True, host="0.0.0.0")
    started.append(gen)
    alpha = next(gen)
    servers.append(alpha)
    for name in ("Bravo", "Charlie"):
        gen = _start(
            tmp_path, "--peer", f"127.0.0.1:{alpha.url.rsplit(':', 1)[1]}", name=name, owner=True
        )
        started.append(gen)
        servers.append(next(gen))
    yield servers
    for gen in started:
        for _ in gen:  # run each generator's cleanup
            pass


@pytest.fixture
def second_page(browser):  # type: ignore[no-untyped-def]
    context = browser.new_context()
    pg = context.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    yield pg
    context.close()
    assert errors == [], f"browser errors: {errors}"


@pytest.fixture
def third_page(browser):  # type: ignore[no-untyped-def]
    context = browser.new_context()
    pg = context.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    yield pg
    context.close()
    assert errors == [], f"browser errors: {errors}"
