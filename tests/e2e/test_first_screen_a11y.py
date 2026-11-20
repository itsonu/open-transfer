"""The first screen a new person sees: readable, easy to hit with a thumb, and clear
about what to do. Measured in a real browser, light and dark, on a phone and a desktop."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.test_ui import Server, _start, sync_api

pytestmark = pytest.mark.e2e
expect = sync_api.expect

LOW_CONTRAST = """(sel) => {
  const root = document.querySelector(sel) || document.body;
  const shown = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden'
      && !e.closest('[aria-hidden="true"]') && !e.closest('[hidden]')
      && (() => { for (let n = e; n; n = n.parentElement) if (getComputedStyle(n).opacity === '0') return false; return true; })(); };
  const lum = (c) => { const [r, g, b] = c.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
  const parse = (s) => { const m = s.match(/rgba?\\(([^)]+)\\)/); if (!m) return null; const p = m[1].split(/[ ,\\/]+/).filter(Boolean).map(Number); return { c: p.slice(0, 3), a: p.length > 3 ? p[3] : 1 }; };
  const mix = (fg, bg) => fg.c.map((v, i) => Math.round(v * fg.a + bg[i] * (1 - fg.a)));
  const background = (e) => { const stack = []; for (let n = e; n; n = n.parentElement) { const p = parse(getComputedStyle(n).backgroundColor); if (p && p.a > 0) stack.push(p); if (p && p.a > 0.95) break; }
    return stack.reduceRight((under, p) => mix(p, under), [255, 255, 255]); };
  const bad = [];
  for (const e of root.querySelectorAll('*')) {
    if (![...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim()) || !shown(e)) continue;
    const s = getComputedStyle(e), fg = parse(s.color), bg = background(e);
    const l1 = lum(mix(fg, bg)), l2 = lum(bg), ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const px = parseFloat(s.fontSize), large = px >= 24 || (px >= 18.66 && parseInt(s.fontWeight) >= 700);
    if (ratio < (large ? 3 : 4.5)) bad.push(`${ratio.toFixed(2)} ${e.tagName}.${e.className} "${e.textContent.trim().slice(0, 30)}"`);
  }
  return bad;
}"""

SMALL_TARGETS = """(sel) => [...(document.querySelector(sel) || document.body)
    .querySelectorAll('button,a[href],input:not([hidden]),select,textarea,[role=button],[role=tab],[role=menuitem],summary')]
  .filter((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden' && !e.closest('.skip-link, .row-title'); })
  .map((e) => [e, (e.closest('label') || e).getBoundingClientRect()])
  .filter(([, r]) => r.width < 44 || r.height < 44)
  .map(([e, r]) => `${e.tagName}#${e.id}.${e.className} ${Math.round(r.width)}x${Math.round(r.height)} ${(e.getAttribute('aria-label') || e.textContent).trim().slice(0, 25)}`)"""


@pytest.fixture
def device(tmp_path: Path) -> Iterator[Server]:
    yield from _start(tmp_path, name="Mac", owner=True, host="0.0.0.0")


def open_first_screen(browser, url: str, **context):  # type: ignore[no-untyped-def]
    page = browser.new_context(**context).new_page()
    page.goto(url)
    expect(page.locator("#connection")).to_have_attribute("data-state", "online")
    expect(page.locator("#nearby-empty")).to_be_visible()
    return page


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_word_on_the_first_screen_is_readable(browser, device: Server, scheme: str) -> None:  # type: ignore[no-untyped-def]
    page = open_first_screen(browser, device.url, color_scheme=scheme)
    assert page.evaluate(LOW_CONTRAST) == []


def test_every_control_is_big_enough_for_a_thumb(browser, device: Server) -> None:  # type: ignore[no-untyped-def]
    page = open_first_screen(
        browser, device.url, viewport={"width": 375, "height": 740}, has_touch=True, is_mobile=True
    )
    assert page.evaluate(SMALL_TARGETS) == []


def test_the_first_screen_explains_itself(browser, device: Server) -> None:  # type: ignore[no-untyped-def]
    page = open_first_screen(
        browser, device.url, viewport={"width": 375, "height": 740}, has_touch=True
    )
    headings = page.evaluate(
        "[...document.querySelectorAll('h1,h2,h3')].filter(e => e.offsetWidth).map(e => e.tagName)"
    )
    assert headings[0] == "H1", f"the page's one big heading comes first, not {headings}"
    assert headings.count("H1") == 1
    empty = page.locator("#nearby-empty").inner_text()
    assert "Looking" not in empty, "it says it is looking while discovery is off"
    assert "Wi" in empty or "code" in empty  # tells you what to do next
    adds = page.get_by_role("button", name="Add device")
    assert sum(b.is_visible() for b in adds.all()) == 1, "one way to add a device, not two"


def test_the_not_seeing_a_device_help_is_readable_and_thumb_sized(browser, device: Server) -> None:  # type: ignore[no-untyped-def]
    page = open_first_screen(
        browser, device.url, viewport={"width": 375, "height": 740}, has_touch=True, is_mobile=True
    )
    page.evaluate(
        "document.querySelector('#nearby-help').hidden = false"
    )  # shown when discovery is on
    page.get_by_text("Not seeing a device?").click()
    expect(page.get_by_text("isn’t a guest network")).to_be_visible()
    assert page.evaluate(LOW_CONTRAST) == []
    assert page.evaluate(SMALL_TARGETS) == []
